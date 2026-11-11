import json
import httpx
from .translations import PHONETIC_FIELDS, TRANSLATION_TYPES, valid_phonetic


ENDPOINT = "https://api.deepseek.com/chat/completions"
SYSTEM_PROMPT = ("你是中英文双向翻译与学习助手。严格按用户给出的翻译方向处理文本，输出完整 JSON，不要 Markdown。"
                 "字段：kind(word 或 sentence), brief_translation(所选目标语言的完整直译), "
                 "brief_usage_zh(只在展开详情显示的用法补充), "
                 "details_zh(详细义项或语法说明和搭配/关键表达), "
                 "part_of_speech(词性，句子可为空), phonetic_us(美式 IPA 音标), phonetic_uk(英式 IPA 音标), "
                 "examples(最多两个对象，每个含 en 英文例句、zh 中文译文)。"
                 "brief_translation 必须逐句翻完并直接给出译文，不得摘要、压缩或省略原意；"
                 "不得解释、评价、标注词性或语法，不得复述原文，也不得写“这是……”“常用于……”等说明。"
                 "句子和短语只给完整直译；单词只给目标语言词义，可用中文分号分隔多个常用义项，"
                 "不要在 brief_translation 中重复音标或词性。"
                 "用法补充和详细解析写入 brief_usage_zh 与 details_zh，绝不能混入 brief_translation。"
                 "kind 为 word 时两种音标都必填，用 /.../ 包裹，包含重音和必要的长音符号；"
                 "即使两种发音相同也分别填写，按当前词义和词性选择常用读音，不用拼音或英文拼读代替 IPA。"
                 "英译中标注原英文单词的音标，中译英标注英文单词译文的音标。"
                 "kind 为 sentence 时两个音标字段都返回空字符串，不给整句标注音标。"
                 "英译中时 brief_translation 必须是中文；中译英时必须是英文。用法与详细说明始终用中文。"
                 "例句提供英文及其中文对照。示例 JSON："
                 '{"kind":"word","brief_translation":"苹果","brief_usage_zh":"可数名词。",'
                 '"details_zh":"一种常见水果。","part_of_speech":"n.",'
                 '"phonetic_us":"/ˈæpəl/","phonetic_uk":"/ˈæpəl/",'
                 '"examples":[{"en":"I ate an apple.","zh":"我吃了一个苹果。"}]}')


class ApiError(Exception):
    pass


def validate_analysis(obj):
    if not isinstance(obj, dict) or obj.get("kind") not in ("word", "sentence"):
        raise ApiError("解析格式不完整，请手动重试。")
    for key in ("brief_translation", "brief_usage_zh", "details_zh"):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ApiError("解析格式不完整，请手动重试。")
    pos = obj.get("part_of_speech")
    if not isinstance(pos, str) or len(pos) > 100 or (obj["kind"] == "word" and not pos.strip()):
        raise ApiError("解析格式不完整，请手动重试。")
    examples = obj.get("examples")
    if not isinstance(examples, list) or len(examples) > 2:
        raise ApiError("解析格式不完整，请手动重试。")
    for ex in examples:
        if not isinstance(ex, dict) or any(not isinstance(ex.get(k), str) or not ex[k].strip() or len(ex[k]) > 1000 for k in ("en", "zh")):
            raise ApiError("解析格式不完整，请手动重试。")
    phonetics = {}
    for field in PHONETIC_FIELDS:
        value = obj.get(field, "")
        if not isinstance(value, str):
            raise ApiError("音标格式不完整，请再次提交重试。")
        value = value.strip()
        if obj["kind"] == "word":
            if not valid_phonetic(value):
                raise ApiError("单词解析缺少有效的美式或英式音标，请再次提交重试。")
        elif value:
            raise ApiError("句子解析的音标格式无效，请再次提交重试。")
        phonetics[field] = value
    return {**{k: obj[k] for k in ("kind", "brief_translation", "brief_usage_zh", "details_zh", "part_of_speech", "examples")},
            **phonetics}


def translate(text, kind, key, model="deepseek-flash", client=None, translation_type="en-zh"):
    if not key:
        raise ApiError("请先在设置中填写 DeepSeek API 密钥。")
    if translation_type not in TRANSLATION_TYPES:
        raise ApiError("请选择英译中或中译英。")
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(25.0, connect=8.0))
    try:
        response = client.post(ENDPOINT, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                               json={"model": model, "stream": False, "thinking": {"type": "disabled"},
                                     "response_format": {"type": "json_object"}, "max_tokens": 1400,
                                     "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                  {"role": "user", "content": f"翻译方向：{TRANSLATION_TYPES[translation_type]}\n类型：{kind}\n原文：{text}"}]})
        if response.status_code == 401:
            raise ApiError("密钥无效，请在设置中检查。")
        if response.status_code == 402:
            raise ApiError("账户余额不足，请检查 DeepSeek 余额。")
        if response.status_code == 429:
            raise ApiError("请求过于频繁，请稍后手动重试。")
        if response.status_code >= 400:
            raise ApiError(f"服务请求失败（HTTP {response.status_code}），请稍后手动重试。")
        payload = response.json()
        choice = payload["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ApiError("解析未完整生成，请手动重试。")
        content = choice["message"]["content"]
        analysis = validate_analysis(json.loads(content))
        if analysis["kind"] != kind:
            raise ApiError("解析类型与提交类型不符，请手动重试。")
        return analysis
    except httpx.TimeoutException:
        raise ApiError("连接超时，原文已保存；请手动重试。") from None
    except httpx.RequestError:
        raise ApiError("网络连接失败，原文已保存；请手动重试。") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ApiError("服务返回的解析无效，原文已保存；请手动重试。") from None
    finally:
        if own_client:
            client.close()
