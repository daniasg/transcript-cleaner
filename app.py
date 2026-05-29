from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
import json
import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL_NAME = os.getenv("MODEL_NAME", "mistral:latest")

# Try to import Whisper for audio transcription
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    print("⚠️  faster_whisper not installed. Audio transcription disabled.")
    print("   To enable: pip install faster-whisper")


def get_whisper_model():
    """Get or create Whisper model (CPU-optimized)"""
    if not WHISPER_AVAILABLE:
        return None
    try:
        model = WhisperModel("base", device="cpu", compute_type="int8")
        return model
    except Exception as e:
        print(f"Error loading Whisper: {e}")
        return None


async def stream_cleanup(transcript: str):
    """Stream cleanup from LOCAL Ollama - 100% offline!"""
    
    prompt = f"""You are a professional transcript editor. Clean up this messy transcript:
- Fix grammar and punctuation
- Remove filler words (um, uh, like, you know, etc.)
- Fix repeated words or sentences
- Add proper capitalization
- Keep original meaning and flow
- Make it professional and readable

Messy transcript:
{transcript}

Cleaned transcript:"""

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream(
                "POST",
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": MODEL_NAME,
                    "prompt": prompt,
                    "stream": True,
                    "temperature": 0.3,
                }
            ) as response:
                async for line in response.aiter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            if "response" in data:
                                yield data["response"]
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            yield f"\n\n❌ ERROR: {str(e)}"


def transcribe_audio(audio_path: str):
    """Transcribe audio file using Whisper"""
    if not WHISPER_AVAILABLE:
        raise Exception("Whisper not available. Install with: pip install faster-whisper")
    
    model = get_whisper_model()
    if not model:
        raise Exception("Failed to load Whisper model")
    
    try:
        segments, info = model.transcribe(audio_path, language="en")
        transcript = " ".join(segment.text for segment in segments)
        return transcript
    except Exception as e:
        raise Exception(f"Transcription error: {str(e)}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/transcribe-text")
async def transcribe_text(data: dict):
    """Clean up text using LOCAL Ollama"""
    transcript = data.get("text", "").strip()
    
    if not transcript:
        raise HTTPException(status_code=400, detail="No text provided")
    
    async def event_generator():
        async for chunk in stream_cleanup(transcript):
            yield f"data: {json.dumps({'token': chunk})}\n\n"
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/transcribe-audio")
async def transcribe_audio_endpoint(file: UploadFile = File(...)):
    """Transcribe audio file with Whisper, then clean with Ollama"""
    
    if not WHISPER_AVAILABLE:
        raise HTTPException(
            status_code=400, 
            detail="Audio transcription not available. Install faster-whisper: pip install faster-whisper"
        )
    
    # Save uploaded file to temp location
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
            contents = await file.read()
            tmp.write(contents)
            tmp_path = tmp.name
        
        # Transcribe
        transcript = transcribe_audio(tmp_path)
        
        # Clean up temp file
        os.unlink(tmp_path)
        
        if not transcript:
            raise HTTPException(status_code=400, detail="No speech detected in audio")
        
        # Return transcript + stream cleanup
        async def event_generator():
            yield f"data: {json.dumps({'transcript': transcript})}\n\n"
            async for chunk in stream_cleanup(transcript):
                yield f"data: {json.dumps({'token': chunk})}\n\n"
        
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
