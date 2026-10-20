"""The generic admin over ``test_db``'s notes, for the history tests."""

from fastapi import APIRouter

from domainkit.contrib.admin import AdminSite
from domainkit_tests.test_db import Note, NotesUnitOfWork

site = AdminSite()
site.register(
    Note,
    unit_of_work=NotesUnitOfWork,
    repository=lambda uow: uow.notes,
    label="alpha.note",
)

router = APIRouter()
router.include_router(site.router, prefix="/admin")
