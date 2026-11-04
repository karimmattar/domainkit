"""The three routes, as async views; a form checks the body."""

import hmac
import json
import os
from collections.abc import Awaitable, Callable
from functools import wraps

from django import forms
from django.http import HttpRequest, HttpResponse, JsonResponse

from .models import Item

API_KEY = os.environ["COMPARE_API_KEY"]
type View = Callable[..., Awaitable[HttpResponse]]


class ItemForm(forms.Form):
    """A new item."""

    name = forms.CharField(min_length=1, max_length=50)
    price = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2)


def keyed(view: View) -> View:
    """Refuse a request without the service's key."""

    @wraps(view)
    async def checked(
        request: HttpRequest, *args: object, **kwargs: object
    ) -> HttpResponse:
        if not hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY):
            return JsonResponse(
                {"detail": "A valid X-API-Key is required."}, status=401
            )
        return await view(request, *args, **kwargs)

    return checked


def out(item: Item) -> dict[str, str]:
    """An item as JSON."""
    return {"id": str(item.id), "name": item.name, "price": str(item.price)}


@keyed
async def read(request: HttpRequest, item_id: str) -> HttpResponse:
    """One item."""
    item = await Item.objects.filter(id=item_id).afirst()
    if item is None:
        return JsonResponse({"detail": "No item has that id."}, status=404)
    return JsonResponse(out(item))


@keyed
async def items(request: HttpRequest) -> HttpResponse:
    """The 20 newest items, or a new one."""
    if request.method == "POST":
        form = ItemForm(json.loads(request.body or b"{}"))
        if not form.is_valid():
            return JsonResponse({"detail": form.errors}, status=422)
        item = await Item.objects.acreate(**form.cleaned_data)
        return JsonResponse(out(item), status=201)
    newest = Item.objects.order_by("-created_at", "id")[:20]
    return JsonResponse({"items": [out(item) async for item in newest]})
