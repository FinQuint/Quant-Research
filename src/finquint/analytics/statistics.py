def simple_return(current_value, previous_value):
    if previous_value == 0:
        raise ValueError("previous_value cannot be zero")
    return current_value / previous_value - 1
