import re


TRANSLATION_TYPES = {"en-zh": "英译中", "zh-en": "中译英"}
PHONETIC_FIELDS = ("phonetic_us", "phonetic_uk")


def type_name(value):
    return TRANSLATION_TYPES.get(value, value)


def brief_translation(analysis):
    # Existing libraries keep their original JSON; new replies use a neutral key.
    return analysis.get("brief_translation", analysis.get("brief_translation_zh", ""))


def valid_phonetic(value):
    return (isinstance(value, str) and len(value) <= 160
            and re.fullmatch(r"/[^/\[\]<>]+/", value) is not None
            and bool(value[1:-1].strip())
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))


def has_word_phonetics(analysis):
    return all(valid_phonetic(analysis.get(field)) for field in PHONETIC_FIELDS)
