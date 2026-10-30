"""Recognize image operations from their endpoint, without reading image data."""
from urllib.parse import urlsplit


IMAGE_REQUEST_KINDS = {"/images/generations": "image_generation", "/images/edits": "image_edit"}


def image_request_kind(path):
    endpoint = urlsplit(path).path.rstrip("/")
    return next((kind for suffix, kind in IMAGE_REQUEST_KINDS.items() if endpoint.endswith(suffix)), None)


def is_json_image_request(path, headers):
    content_type = next((value for name, value in headers.items() if name.lower() == "content-type"), "")
    return (image_request_kind(path) is not None
            and content_type.split(";", 1)[0].strip().lower() == "application/json")
