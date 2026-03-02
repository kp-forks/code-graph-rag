import sys

import atheris


def fuzz_language_spec(data):
    fdp = atheris.FuzzedDataProvider(data)
    extension = fdp.ConsumeUnicodeNoSurrogates(64)
    from codebase_rag.language_spec import (
        get_language_for_extension,
        get_language_spec,
    )

    try:
        get_language_spec(extension)
    except Exception:
        pass

    try:
        get_language_for_extension(extension)
    except Exception:
        pass


if __name__ == "__main__":
    atheris.Setup(sys.argv, fuzz_language_spec)
    atheris.Fuzz()
