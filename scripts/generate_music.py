import json
import time
from pathlib import Path

import requests
from mamush_film.config import load_config

config = load_config()
base_url = config.acestep.api_url.rstrip("/")
api_key = config.acestep.api_key

if not base_url or not api_key:
    raise SystemExit("Missing ACE-Step API URL or key in .env")

model = "fal-ai/ace-step/prompt-to-audio"
headers = {
    "Authorization": f"Key {api_key}",
    "Content-Type": "application/json",
}

prompt = """
Original emotional cinematic instrumental soundtrack for an Ethiopian
short film about a mother's unconditional love for her son.
Ethiopian traditional acoustic influences: warm krar lyre plucking,
expressive masinko bowed strings, gentle washint bamboo flute.
76 BPM, intimate organic acoustic arrangement, cinematic orchestral
textures, beautiful memorable melody, no electronic dance beat.
Begin with tender peaceful dawn and delicate sparse instruments.
Gradually introduce tension and emotional intensity around the middle.
Build into a deeply moving warm emotional crescendo representing
sacrifice and unconditional maternal love.
Finish with a hopeful, uplifting, peaceful resolution.
Professional film score, natural dynamics, seamless musical development.
No vocals, no singing, no lyrics, no speech, instrumental only.
"""

payload = {
    "prompt": prompt,
    "instrumental": True,
    "duration": 81,
    "number_of_steps": 27,
    "enable_safety_checker": True,
}

print("Submitting ACE-Step music generation request...")
response = requests.post(
    base_url,
    headers=headers,
    json=payload,
    timeout=60,
)
if not response.ok:
    print("HTTP STATUS:", response.status_code)
    print("API ERROR:", response.text[:2000])
    raise SystemExit("fal.ai rejected the request. See error above.")

job = response.json()

request_id = job.get("request_id")
if not request_id:
    raise RuntimeError(f"No request_id returned: {job}")

print("Request ID:", request_id)

status_url = f"{base_url}/requests/{request_id}/status"
result_url = f"{base_url}/requests/{request_id}"

while True:
    response = requests.get(
        status_url,
        headers=headers,
        params={"logs": "true"},
        timeout=60,
    )
    response.raise_for_status()
    status = response.json()
    state = status.get("status")

    print("Generation status:", state)

    if state == "COMPLETED":
        break
    if state in ("FAILED", "CANCELLED"):
        raise RuntimeError(json.dumps(status, indent=2))

    time.sleep(5)

response = requests.get(result_url, headers=headers, timeout=60)
response.raise_for_status()
result = response.json()

audio = result.get("audio", {})
audio_url = audio.get("url") if isinstance(audio, dict) else None

if not audio_url:
    raise RuntimeError(f"Audio URL missing from result: {result}")

output = config.paths.music_dir / "mamush_original_score.wav"
output.parent.mkdir(parents=True, exist_ok=True)

print("Downloading generated soundtrack...")
with requests.get(audio_url, stream=True, timeout=180) as download:
    download.raise_for_status()
    with output.open("wb") as file:
        for chunk in download.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file.write(chunk)

print("\nSUCCESS")
print("Audio:", output)
print("Seed:", result.get("seed"))
print("Tags:", result.get("tags"))
