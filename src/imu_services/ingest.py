"""Bounded NDJSON decoding. Staging is temporary; acceptance requires a DB commit."""
import hashlib
import json
import math
import tempfile
import zlib
import numpy as np
from imu_inference import InputBatch
from .codec import ROW_DTYPE
from .models import WholeRecordHeader


class UploadTooLarge(ValueError):
    pass


class RecordUpload:
    def __init__(self, settings, encoding="identity"):
        if encoding not in ("identity", "gzip"):
            raise ValueError("Content-Encoding must be identity or gzip")
        self.settings = settings
        self.decoder = zlib.decompressobj(16+zlib.MAX_WBITS) if encoding == "gzip" else None
        self.spool = tempfile.TemporaryFile()
        self.header = None
        self.raw_bytes = self.decoded_bytes = self.count = 0
        self.buffer = bytearray()
        self.rows = []
        self.digest = hashlib.sha256()

    def feed(self, data):
        self.raw_bytes += len(data)
        if self.raw_bytes > self.settings.max_whole_body_bytes:
            raise UploadTooLarge("complete-record upload byte limit exceeded")
        for start in range(0, len(data), 65536):
            part = data[start:start+65536]
            if self.decoder is None:
                self._decoded(part)
                continue
            while part:
                try:
                    decoded = self.decoder.decompress(part, 65536)
                except zlib.error as exc:
                    raise ValueError("invalid gzip input") from exc
                self._decoded(decoded)
                if self.decoder.unused_data:
                    raise ValueError("trailing data or multiple gzip members are unsupported")
                part = self.decoder.unconsumed_tail

    def _decoded(self, data):
        self.decoded_bytes += len(data)
        if self.decoded_bytes > self.settings.max_whole_decoded_bytes:
            raise UploadTooLarge("decompressed input byte limit exceeded")
        self.buffer.extend(data)
        start = 0
        while (end := self.buffer.find(b"\n", start)) >= 0:
            line = self.buffer[start:end]
            self._line(line)
            start = end+1
        if start:
            del self.buffer[:start]
        if len(self.buffer) > 4096:
            raise UploadTooLarge("NDJSON line exceeds 4096 bytes")

    def _line(self, line):
        if not line or len(line) > 4096:
            raise ValueError("NDJSON requires nonempty lines of at most 4096 bytes")
        try:
            def pairs(items):
                out = {}
                for key, value in items:
                    if key in out:
                        raise ValueError("duplicate JSON member")
                    out[key] = value
                return out
            row = json.loads(line, object_pairs_hook=pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid UTF-8 JSON line") from exc
        if self.header is None:
            self.header = WholeRecordHeader.model_validate(row)
            return
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError("sample row must contain ax,ay,az,gx,gy,gz,valid,excluded")
        if (any(type(x) not in (int, float) or abs(x) > 3.402823466e38 or not math.isfinite(x)
                for x in row[:6]) or any(type(x) is not bool for x in row[6:])):
            raise ValueError("samples require six finite float32 values and two booleans")
        self.count += 1
        if self.count > self.header.sample_count:
            raise ValueError("more samples than declared; recordings must not exceed 25 hours")
        self.rows.append(row)
        if len(self.rows) >= 15000:
            self._flush()

    def _flush(self):
        if not self.rows:
            return
        arr = np.empty(len(self.rows), ROW_DTYPE)
        arr["values"] = [r[:6] for r in self.rows]
        arr["valid"] = [r[6] for r in self.rows]
        arr["excluded"] = [r[7] for r in self.rows]
        payload = arr.tobytes()
        self.digest.update(payload)
        self.spool.write(payload)
        self.rows.clear()

    def finish(self):
        if self.decoder is not None and not self.decoder.eof:
            raise ValueError("truncated gzip stream")
        if self.buffer:
            self._line(self.buffer)
            self.buffer.clear()
        if self.header is None or self.count != self.header.sample_count:
            raise ValueError("complete-record sample count mismatch")
        self._flush()
        self.spool.seek(0)
        return self

    def batches(self):
        self.spool.seek(0)
        start = 0
        while data := self.spool.read(15000*ROW_DTYPE.itemsize):
            rows = np.frombuffer(data, ROW_DTYPE)
            x = rows["values"]
            yield InputBatch.create(x[:, :3], angular_velocity=x[:, 3:], start=start,
                                    valid=rows["valid"], excluded=rows["excluded"])
            start += len(rows)

    def close(self):
        self.spool.close()
