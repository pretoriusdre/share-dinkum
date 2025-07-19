
# TODO implement logging

from django.forms.models import model_to_dict




def save_with_logging(obj, context=""):
    try:
        obj.save()
    except Exception as e:
        print(f"Error saving object of type {type(obj).__name__} in context: {context}")
        print(f"Exception: {e}")
        print("Object data:")
        
        data = model_to_dict(obj)
        for k, v in data.items():
            val_str = f"{v} ({type(v).__name__})"
            obj_id = getattr(v, 'id', None)
            if obj_id:
                val_str += f" (ID: {obj_id})"
            print(f" - {k}: {val_str}")
        
        raise e