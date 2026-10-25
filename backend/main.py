from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.model.inference import predict

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class InferenceRequest(BaseModel):
    inputs: dict[str, Any]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/inference")
def inference(request: InferenceRequest) -> dict[str, Any]:
    try:
        return predict(request.inputs)
    except NotImplementedError as error:
        raise HTTPException(status_code=501, detail=str(error)) from error