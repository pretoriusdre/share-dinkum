from django.apps import AppConfig


class ShareTrackerAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'share_dinkum_app'

    def ready(self):
        import share_dinkum_app.signals  # noqa