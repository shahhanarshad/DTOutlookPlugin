import functools
import re
import time


def exception_logger(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as e:
            args[0].logger.exception(f'Error executing {function.__name__}: "{e}"')
            raise

    return wrapper


def time_it(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        start = time.time()
        ret = function(*args, **kwargs)
        delta = time.time() - start

        args[0].logger.info(
            f"Execution of {function.__name__} took {delta:.3f} seconds"
        )

        return ret

    return wrapper


def camel_to_snake(name):
    return "_".join(re.findall("[A-Z][^A-Z]*", name)).lower()


def snake_to_camel(word):
    return "".join(x.capitalize() or "_" for x in word.split("_"))
