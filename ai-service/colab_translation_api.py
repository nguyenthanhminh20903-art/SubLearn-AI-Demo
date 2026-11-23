import argparse
import json
import os
from functools import lru_cache
from pathlib import Path
from secrets import compare_digest

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


MODEL_NAME = os.getenv("SUBTITLE_COLAB_TRANSLATION_MODEL", "Qwen/Qwen2.5-3B-Instruct")
app = FastAPI(title="SubLearn Colab Translation API", docs_url=None, redoc_url=None)


@lru_cache(maxsize=1)
def load_model():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME, torch_dtype=torch.float16 if device == 'cuda' else torch.float32,
        device_map='auto' if device == 'cuda' else None,
    )
    model.eval()
    return torch, tokenizer, model


class ChatMessage(BaseModel):
    role: str = Field(max_length=20)
    content: str = Field(max_length=50000)


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=8)
    temperature: float = Field(default=0, ge=0, le=1)
    max_tokens: int = Field(default=2048, ge=1, le=2048)


@app.post("/v1/chat/completions")
def chat_completions(request: ChatRequest, authorization: str = Header(default='')):
    token = os.getenv('SUBTITLE_CONTEXT_TRANSLATION_TOKEN', '')
    if not token or not compare_digest(authorization, f'Bearer {token}'):
        raise HTTPException(401, 'Token required')
    return generate(request)


def generate(request):
    if request.model and request.model != MODEL_NAME:
        raise HTTPException(422, 'Unknown translation model')
    torch, tokenizer, model = load_model()
    prompt_messages = [
        {"role": item.role, "content": item.content}
        for item in request.messages
    ]
    text = tokenizer.apply_chat_template(
        prompt_messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    inputs = tokenizer(text, return_tensors="pt").to(model.device)
    if inputs.input_ids.shape[1] > 8192:
        raise HTTPException(422, 'Subtitle context is too long')
    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=request.max_tokens,
            do_sample=request.temperature > 0,
            temperature=request.temperature if request.temperature > 0 else None,
        )
    generated = tokenizer.decode(output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": generated},
        }],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, help='Read a ChatRequest JSON file for a notebook experiment.')
    parser.add_argument('--output', type=Path, default=Path('translation-results.json'))
    parser.add_argument('--port', type=int, default=8002)
    options = parser.parse_args()
    if options.input:
        request = ChatRequest.model_validate_json(options.input.read_text(encoding='utf-8'))
        options.output.write_text(json.dumps(generate(request), ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        if not os.getenv('SUBTITLE_CONTEXT_TRANSLATION_TOKEN'):
            parser.error('Set SUBTITLE_CONTEXT_TRANSLATION_TOKEN before serving the translation endpoint.')
        import uvicorn

        uvicorn.run(app, host='127.0.0.1', port=options.port)
