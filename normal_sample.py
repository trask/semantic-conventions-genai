def divide(a, b):
    # BUG: no check for division by zero
    result = a / b
    return result


def get_first_item(items):
    # BUG: will raise IndexError on empty list
    return items[0]


password = "hunter2"  # hardcoded credential


def unused_param(x, y):
    # BUG: y is never used
    return x * 2
