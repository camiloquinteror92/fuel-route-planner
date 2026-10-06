# Price spread per station (min / max of its quotes) and removal of two indexes
# that no query used. Existing rows get 0 for the new columns; run
# `manage.py load_stations` afterwards to fill them (it replaces every row).

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fuelroute", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelstation",
            name="price_min",
            field=models.DecimalField(decimal_places=4, default=Decimal("0"), max_digits=7),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="fuelstation",
            name="price_max",
            field=models.DecimalField(decimal_places=4, default=Decimal("0"), max_digits=7),
            preserve_default=False,
        ),
        migrations.RemoveIndex(
            model_name="fuelstation",
            name="station_lat_lon_idx",
        ),
        migrations.RemoveIndex(
            model_name="fuelstation",
            name="station_state_city_idx",
        ),
    ]
