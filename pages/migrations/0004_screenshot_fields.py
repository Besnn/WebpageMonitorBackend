"""Add screenshot_enabled to MonitoredPage and screenshot/diff fields to MonitoredPageCheck."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pages', '0003_add_settings_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='monitoredpage',
            name='screenshot_enabled',
            field=models.BooleanField(default=False, help_text='Capture a screenshot on each check'),
        ),
        migrations.AddField(
            model_name='monitoredpagecheck',
            name='screenshot_path',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
        migrations.AddField(
            model_name='monitoredpagecheck',
            name='diff_path',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
        migrations.AddField(
            model_name='monitoredpagecheck',
            name='diff_score',
            field=models.FloatField(
                blank=True,
                null=True,
                help_text='Visual change score 0-100. 0 = identical, 100 = completely different.',
            ),
        ),
    ]

