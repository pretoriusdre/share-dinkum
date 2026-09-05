"""Rename the instrument TAP field to say that it is an override.

`makemigrations` offers a remove-and-add for this, which drops the column and everything in
it. Written by hand as a rename so that a legitimate True -- an instrument that really is
taxable Australian property in its own right -- survives the upgrade.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('share_dinkum_app', '0023_alter_instrument_cgt_asset_category_override'),
    ]

    operations = [
        migrations.RenameField(
            model_name='instrument',
            old_name='is_taxable_australian_property',
            new_name='is_taxable_australian_property_override',
        ),
        migrations.AlterField(
            model_name='instrument',
            name='is_taxable_australian_property_override',
            field=models.BooleanField(
                blank=True,
                null=True,
                help_text='Leave empty. Only set this if the instrument is taxable '
                          'Australian property in its own right -- real property, or a '
                          'non-portfolio interest in a land rich entity. Setting it to '
                          '"no" is not the same as leaving it empty: it overrides the '
                          'departure deeming for every parcel, including ones you held '
                          'when you ceased Australian residency.',
            ),
        ),
    ]
