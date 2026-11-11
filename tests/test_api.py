import json
import httpx
import pytest

from wordbook.api import ApiError, translate


VALID = {"kind": "word", "brief_translation": "苹果", "brief_usage_zh": "可数名词",
         "details_zh": "一种水果", "part_of_speech": "n.", "phonetic_us": "/ˈæpəl/", "phonetic_uk": "/ˈæpəl/",
         "examples": [{"en": "An apple.", "zh": "一个苹果。"}]}


def client_for(status=200, content=None, reason="stop"):
    def handler(request):
        assert request.url.path == "/chat/completions"
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(status, json={"choices": [{"finish_reason": reason, "message": {"content": content if content is not None else json.dumps(VALID)}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success():
    with client_for() as client:
        result = translate("apple", "word", "secret", client=client)
        assert result["brief_translation"] == "苹果"
        assert result["phonetic_us"] == result["phonetic_uk"] == "/ˈæpəl/"


@pytest.mark.parametrize("status,fragment", [(401, "密钥"), (402, "余额"), (429, "频繁"), (500, "HTTP 500")])
def test_http_errors(status, fragment):
    with client_for(status) as client, pytest.raises(ApiError, match=fragment):
        translate("apple", "word", "secret", client=client)


@pytest.mark.parametrize("content,reason", [("broken", "stop"), ("{}", "stop"), (json.dumps(VALID), "length")])
def test_invalid(content, reason):
    with client_for(content=content, reason=reason) as client, pytest.raises(ApiError):
        translate("apple", "word", "secret", client=client)


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timeout")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(ApiError, match="超时"):
        translate("apple", "word", "secret", client=client)


def test_model_type_mismatch_is_rejected():
    payload = {**VALID, "kind": "sentence", "phonetic_us": "", "phonetic_uk": ""}
    with client_for(content=json.dumps(payload)) as client, pytest.raises(ApiError, match="类型"):
        translate("apple", "word", "secret", client=client)


def test_chinese_to_english_direction_is_in_request():
    def handler(request):
        body = json.loads(request.content)
        assert "翻译方向：中译英" in body["messages"][1]["content"]
        assert "目标语言" in body["messages"][0]["content"]
        payload = {**VALID, "brief_translation": "apple"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert translate("苹果", "word", "secret", client=client, translation_type="zh-en")["brief_translation"] == "apple"


@pytest.mark.parametrize("field", ["phonetic_us", "phonetic_uk"])
@pytest.mark.parametrize("bad", [None, 12, [], "", "//", "/ /", "apple", "/a\nb/", "/a\x00b/", "/" + "a" * 160 + "/"])
def test_invalid_word_pronunciation_is_not_saved_as_success(field, bad):
    payload = {**VALID, field: bad}
    with client_for(content=json.dumps(payload)) as client, pytest.raises(ApiError, match="音标"):
        translate("apple", "word", "secret", client=client)


@pytest.mark.parametrize("field", ["phonetic_us", "phonetic_uk"])
def test_missing_dialect_is_rejected(field):
    payload = {key: value for key, value in VALID.items() if key != field}
    with client_for(content=json.dumps(payload)) as client, pytest.raises(ApiError, match="音标"):
        translate("apple", "word", "secret", client=client)


def test_different_dialects_and_prompt_in_one_request():
    calls = []
    def handler(request):
        calls.append(request)
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert all(value in prompt for value in ("phonetic_us", "phonetic_uk", "美式 IPA", "英式 IPA"))
        payload = {**VALID, "phonetic_us": " /təˈmeɪtoʊ/ ", "phonetic_uk": "/təˈmɑːtəʊ/"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = translate("tomato", "word", "secret", client=client)
    assert result["phonetic_us"] == "/təˈmeɪtoʊ/"
    assert result["phonetic_uk"] == "/təˈmɑːtəʊ/"
    assert len(calls) == 1


def test_prompt_requires_complete_direct_translation_without_usage_notes():
    def handler(request):
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert all(value in prompt for value in (
            "完整直译", "不得摘要", "不得解释", "绝不能混入 brief_translation"))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(VALID)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        translate("apple", "word", "secret", client=client)


def test_sentences_do_not_require_word_pronunciation():
    payload = {key: value for key, value in VALID.items() if key not in ("phonetic_us", "phonetic_uk")}
    payload.update(kind="sentence", part_of_speech="")
    with client_for(content=json.dumps(payload)) as client:
        result = translate("I ate an apple.", "sentence", "secret", client=client)
    assert result["phonetic_us"] == result["phonetic_uk"] == ""
