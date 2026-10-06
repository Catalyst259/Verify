"""Preserve complete model JSON while escaping literal control characters in strings."""
import json


def normalize_model_json(content: str) -> str:
    """Keep valid JSON unchanged; never repair missing structure or extract fragments.

    Some compatible providers return unescaped newlines inside JSON strings.
    Decode only that lexical extension, then re-encode for normal schema validation.
    """
    try:
        parsed = json.loads(content)
        json.dumps(parsed, allow_nan=False)
        return content
    except ValueError:
        try:
            return json.dumps(json.loads(content, strict=False), ensure_ascii=False, allow_nan=False)
        except ValueError:
            raise ValueError("模型未返回完整的有效 JSON") from None
