# Generated migration for adding settings fields to MonitoredPage

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pages', '0002_monitoredpagecheck'),
    ]

    operations = [
        migrations.AddField(
            model_name='monitoredpage',
            name='check_interval',
            field=models.IntegerField(default=5, help_text='Check interval in minutes (1-60)'),
        ),
        migrations.AddField(
            model_name='monitoredpage',
            name='notifications_enabled',
            field=models.BooleanField(default=False, help_text='Enable notifications for this site'),
        ),
        migrations.AddField(
            model_name='monitoredpage',
            name='alert_threshold',
            field=models.IntegerField(default=3, help_text='Number of consecutive failures before alerting (1-10)'),
        ),
    ]
