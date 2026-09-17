import bcrypt


def verify_password(plain: str, hashed: str | None) -> bool:
    if not plain or not hashed:
        return False
    try:
        stored = hashed.replace("$2y$", "$2b$").encode("utf-8")
        return bcrypt.checkpw(plain.encode("utf-8"), stored)
    except (ValueError, TypeError):
        return False


def hash_password(plain: str) -> str:
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8").replace("$2b$", "$2y$")
