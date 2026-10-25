# Model inference and visualisation skeleton

This repository contains a generic Python API boundary for model inference and a plain JavaScript browser entry point. It does not include a particular model or application workflow.

## Run the backend

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload
```

The backend exposes `GET /health` and `POST /api/inference`. The inference endpoint returns `501` until `backend/model/inference.py` is connected to a model.

## Run the visualisation

In a second terminal from the repository root:

```sh
python3 -m http.server 5500 --directory visualisation
```

Open `http://localhost:5500`. `visualisation/main.js` exports `runInference(inputs)` and `renderVisualization(output)` for the visualization implementation to build on.
