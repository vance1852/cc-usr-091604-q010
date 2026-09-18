"""客服视图的身份摘要脱敏。"""


def mask_name(name: str) -> str:
    """张伟民 -> 张**"""
    if not name:
        return ""
    if len(name) == 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def mask_id_number(id_number: str) -> str:
    """110101199001011234 -> 110************234"""
    if not id_number:
        return ""
    if len(id_number) <= 6:
        return "*" * len(id_number)
    return f"{id_number[:3]}{'*' * (len(id_number) - 6)}{id_number[-3:]}"


def mask_phone(phone: str) -> str:
    """13812345678 -> 138****5678"""
    if not phone:
        return ""
    if len(phone) < 7:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"
