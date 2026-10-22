from uuid import UUID
import numpy as np
import asyncio
from fastapi import Depends, Request, HTTPException
from starlette.concurrency import run_in_threadpool
from .ingest import RecordUpload, UploadTooLarge
from .errors import Overloaded
from imu_inference import InputBatch, capabilities
from .http_common import make_app
from .models import CreateRun, BatchRequest, GapRequest, SealRequest
from .settings import Settings


def create_app(settings=None, store=None):
    settings = settings or Settings.from_env("api")
    app, auth = make_app(settings, "IMU inference admission", store)
    secured = [Depends(auth)]
    app.state.upload_slots = 0

    async def staged_call(function, *args):
        # Cancellation must not close the spool while a parser/DB thread still uses it.
        task = asyncio.create_task(run_in_threadpool(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    @app.post("/v2/runs/whole-record", status_code=202, dependencies=secured)
    async def whole_record(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-ndjson":
            raise HTTPException(415, "use application/x-ndjson")
        encoding = request.headers.get("content-encoding", "identity")
        if encoding not in ("identity", "gzip"):
            raise HTTPException(415, "Content-Encoding must be identity or gzip")
        if app.state.upload_slots >= settings.max_whole_uploads:
            raise Overloaded("complete-record upload slots are occupied")
        app.state.upload_slots += 1
        upload = None
        try:
            upload = RecordUpload(settings, encoding)
            try:
                async with asyncio.timeout(settings.upload_timeout_seconds):
                    async for chunk in request.stream():
                        await staged_call(upload.feed, chunk)
                    await staged_call(upload.finish)
            except TimeoutError:
                raise HTTPException(408, "complete-record upload timed out") from None
            except UploadTooLarge as exc:
                raise HTTPException(413, str(exc)) from None
            run_id, duplicate = await staged_call(app.state.store.create_whole, upload)
            return {"run_id": run_id, "duplicate": duplicate, "status_path": f"/v2/runs/{run_id}"}
        finally:
            if upload is not None:
                upload.close()
            app.state.upload_slots -= 1

    @app.get("/v2/capabilities", dependencies=secured)
    def supported():
        result = capabilities()
        result["steps"]["deployment_enabled"] = settings.steps_enabled
        result["transport"] = {"max_batch_samples": settings.max_batch_samples,
                               "max_body_bytes": settings.max_body_bytes,
                               "whole_record_max_wire_bytes": settings.max_whole_body_bytes,
                               "whole_record_max_decoded_bytes": settings.max_whole_decoded_bytes,
                               "whole_record_upload_timeout_seconds": settings.upload_timeout_seconds,
                               "sign_adaptation_enabled": settings.sign_adaptation,
                               "partial_overlap": "reject",
                               "input_contract": "immutable_standardized_25hz"}
        return result

    @app.post("/v2/runs", status_code=202, dependencies=secured)
    def create(body: CreateRun):
        payload = body.model_dump()
        payload["start_time"] = body.start_time.isoformat()
        run_id, duplicate = app.state.store.create(payload)
        return {"run_id": run_id, "duplicate": duplicate, "status_path": f"/v2/runs/{run_id}"}

    @app.post("/v2/runs/{run_id}/batches", status_code=202, dependencies=secured)
    def batch(run_id: UUID, body: BatchRequest):
        value = InputBatch.create(body.acceleration_mps2, start=body.start_sample,
                                  valid=body.valid, excluded=body.excluded,
                                  angular_velocity=body.angular_velocity_rads)
        return app.state.store.accept(run_id, body.batch_id, value)

    @app.post("/v2/runs/{run_id}/gaps", status_code=202, dependencies=secured)
    def gap(run_id: UUID, body: GapRequest):
        n = body.sample_count
        value = InputBatch.create(np.zeros((n, 3), np.float32), start=body.start_sample,
                                  angular_velocity=np.zeros((n, 3), np.float32),
                                  valid=np.zeros(n, bool), excluded=np.full(n, body.excluded, bool))
        return app.state.store.accept(run_id, body.batch_id, value)

    @app.post("/v2/runs/{run_id}/seal", status_code=202, dependencies=secured)
    def seal(run_id: UUID, body: SealRequest):
        app.state.store.seal(run_id, body.end_sample)
        return {"run_id": run_id, "sealed_end": body.end_sample}

    @app.post("/v2/runs/{run_id}/retry", status_code=202, dependencies=secured)
    def retry(run_id: UUID):
        app.state.store.retry(run_id)
        return {"run_id": run_id, "status": "queued"}

    @app.get("/metrics", dependencies=secured)
    def metrics():
        from fastapi.responses import Response
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        return Response(generate_latest(app.state.metrics), media_type=CONTENT_TYPE_LATEST)

    return app
