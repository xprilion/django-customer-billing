# Generated by Django 2.0.1 on 2018-01-27 06:13

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0011_cascade_delete_product_properties'),
    ]

    operations = [
        migrations.AlterField(
            model_name='charge',
            name='deleted',
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
