
# TODO implement logging

from django.forms.models import model_to_dict

import logging
logger = logging.getLogger(__name__)


def save_with_logging(obj, context=""):
    try:
        obj.save()
    except Exception as e:
        logger.error(f"Error saving object of type {type(obj).__name__} in context: {context}")
        logger.error(f"Exception: {e}", exc_info=True)
        logger.error("Object data:")

        data = model_to_dict(obj)
        for k, v in data.items():
            val_str = f"{v} ({type(v).__name__})"
            obj_id = getattr(v, 'id', None)
            if obj_id:
                val_str += f" (ID: {obj_id})"
            logging.info(f" - {k}: {val_str}")
        
        raise