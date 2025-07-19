from decimal import Decimal, ROUND_DOWN

def convert_to_decimal(value, max_digits, decimal_places):
    if value is None or str(value).strip().lower() == 'nan':
        return None
    try:
        decimal_value = Decimal(value)
        quantizer = Decimal('1.' + '0' * decimal_places)
        decimal_value = decimal_value.quantize(quantizer, rounding=ROUND_DOWN)

        # Truncate if value exceeds max_digits
        digits_only = str(decimal_value).replace('.', '').replace('-', '')
        if len(digits_only) > max_digits:
            # Truncate the number by adjusting the quantizer
            allowed_integer_digits = max_digits - decimal_places
            if allowed_integer_digits <= 0:
                raise ValueError(f"Cannot truncate: max_digits ({max_digits}) too small for decimal_places ({decimal_places})")

            # Convert to string to manually truncate the integer part
            sign, digits, exponent = decimal_value.as_tuple()
            digits_str = ''.join(map(str, digits))
            int_part = digits_str[:allowed_integer_digits]
            frac_part = digits_str[allowed_integer_digits:allowed_integer_digits+decimal_places]
            new_str = f"{'-' if sign else ''}{int_part or '0'}.{frac_part.ljust(decimal_places, '0')}"
            decimal_value = Decimal(new_str)

        return decimal_value
    except Exception as e:
        raise ValueError(f"Error converting value {value} to Decimal: {e}")