from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pages', '0007_add_crop_path'),
    ]

    operations = [
        migrations.AddField(
            model_name='monitoredpage',
            name='is_pinned',
            field=models.BooleanField(default=False, help_text='Pinned sites stay at the top of the dashboard'),
        ),
    ]

