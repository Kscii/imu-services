import io
import numpy as np
from imu_inference import InputBatch

ROW_DTYPE = np.dtype([("values", "<f4", (6,)), ("valid", "?"), ("excluded", "?")])


def canonical_rows(batch):
    rows = np.empty(len(batch.acceleration), ROW_DTYPE)
    rows["values"][:, :3] = batch.acceleration
    rows["values"][:, 3:] = batch.angular_velocity
    rows["valid"], rows["excluded"] = batch.valid, batch.excluded
    return rows.tobytes()


def encode_batch(batch):
    out = io.BytesIO()
    values = dict(start=np.array(batch.start), acceleration=batch.acceleration,
                  valid=batch.valid, excluded=batch.excluded)
    if batch.angular_velocity is not None:
        values["angular_velocity"] = batch.angular_velocity
    np.savez_compressed(out, **values)
    return out.getvalue()


def decode_batch(payload):
    # Database-owned payload, never a caller-provided pickle.
    with np.load(io.BytesIO(payload), allow_pickle=False) as z:
        return InputBatch(int(z["start"]), z["acceleration"], z["valid"], z["excluded"],
                          z["angular_velocity"])
