"""Helpers that test files share.

A helper more than one test file needs lives here, by area, not in the
test file that used it first: importing from a test file ties two files
that are read, moved and renamed apart.
"""
