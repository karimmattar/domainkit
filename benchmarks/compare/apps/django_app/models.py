"""The item, on the shared table."""

import uuid

from django.db import models


class Item(models.Model):
    """Something for sale."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    version = models.IntegerField(default=1)
    name = models.CharField(max_length=50)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """The table every compared app shares."""

        db_table = "compare_items"
        managed = False
