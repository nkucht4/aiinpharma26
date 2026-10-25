const inferenceEndpoint = "http://localhost:8000/api/inference";

export async function runInference(inputs) {
  const response = await fetch(inferenceEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ inputs }),
  });

  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.detail ?? `Inference request failed (${response.status})`);
  }

  return result;
}

export function renderVisualization(output, container = document.querySelector("#visualization")) {
  if (!container) {
    throw new Error("Visualization container was not found.");
  }

  container.textContent = JSON.stringify(output, null, 2);
}