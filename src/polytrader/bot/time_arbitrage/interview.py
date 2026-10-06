

def decorator(f):

    def wrapper(*args, **kwargs):
        print("Starting method")
        result = f(*args, **kwargs)
        print("Ending method call")
        return result

    return wrapper

@decorator
def myCoolFunc():
    print("hello world")