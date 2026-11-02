"""Provider presets for direct and optional gateway model connections."""

from __future__ import annotations


PROVIDER_PRESETS = {
    "openai_compatible": {
        "label": "OpenAI 兼容",
        "protocol": "openai",
        "default_base_url": "",
        "supports_model_catalog": True,
    },
    "volcengine_ark": {
        "label": "火山方舟文本",
        "protocol": "openai",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "supports_model_catalog": True,
    },
    "volcengine_seedance": {
        "label": "火山方舟视频生成",
        "protocol": "doubao_video",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "supports_model_catalog": True,
    },
    "doubao_tts": {
        "label": "豆包 TTS",
        "protocol": "volcengine_tts",
        "default_base_url": "https://openspeech.bytedance.com/api/v1/tts",
        "supports_model_catalog": False,
        "supports_voice_catalog": True,
        "tts_resources": (
            {"id": "seed-tts-1.0", "label": "豆包语音合成 1.0"},
            {"id": "seed-tts-2.0", "label": "豆包语音合成 2.0"},
        ),
    },
    "new_api": {
        "label": "New API（高级兼容）",
        "protocol": "openai",
        "default_base_url": "http://127.0.0.1:3000",
        "supports_model_catalog": True,
    },
}


def provider_preset(preset_id: str) -> dict:
    if not isinstance(preset_id, str) or preset_id not in PROVIDER_PRESETS:
        raise ValueError("未知供应商预设")
    return dict(PROVIDER_PRESETS[preset_id])
