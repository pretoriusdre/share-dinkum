from decimal import Decimal, ROUND_HALF_UP

def convert_to_decimal(value, max_digits, decimal_places):
    if value is None or str(value).strip().lower() == 'nan':
        return None
    try:
        # Convert via string to avoid float artifacts
        decimal_value = Decimal(str(value))

        # Proper quantizer (e.g. 0.0001 for 4 decimal places)
        quantizer = Decimal("1") / (Decimal(10) ** decimal_places)
        
        # Round safely
        decimal_value = decimal_value.quantize(quantizer, rounding=ROUND_HALF_UP)

        # Check max_digits
        digits_only = str(decimal_value).replace('.', '').replace('-', '')
        if len(digits_only) > max_digits:
            allowed_integer_digits = max_digits - decimal_places
            int_digits = len(str(decimal_value.to_integral_value(ROUND_HALF_UP)))
            if int_digits > allowed_integer_digits:
                raise ValueError(
                    f"Integer part too large for max_digits={max_digits} and decimal_places={decimal_places}: {decimal_value}"
                )
        return decimal_value

    except Exception as e:
        raise ValueError(f"Error converting value {value} to Decimal: {e}")