import os
import re
import shutil
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ..config import settings


def _configure_tesseract(pytesseract) -> None:
    command = settings.tesseract_cmd or shutil.which("tesseract")
    if not command and os.name == "nt":
        for candidate in (
            Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
            Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
        ):
            if candidate.is_file():
                command = str(candidate)
                break
    if command:
        pytesseract.pytesseract.tesseract_cmd = command


def validate_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
        if image.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("Only JPEG, PNG, and WEBP images are supported.")
        image.verify()
        image = Image.open(BytesIO(data))
        if image.width * image.height > 25_000_000:
            raise ValueError("The image dimensions exceed the supported limit.")
        return image.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The uploaded file is not a valid supported image.") from exc


@lru_cache(maxsize=1)
def _yolo_model():
    if not settings.yolo_model_path:
        return None
    from ultralytics import YOLO

    return YOLO(settings.yolo_model_path)


def detect_objects(data: bytes) -> list[dict]:
    image = validate_image(data)
    model = _yolo_model()
    if model is None:
        raise RuntimeError("YOLO is unavailable: set YOLO_MODEL_PATH to a local model file.")
    results = model.predict(image, verbose=False)
    detections = []
    for result in results:
        names = result.names
        for box in result.boxes:
            confidence = float(box.conf[0].item())
            coords = [round(float(value), 2) for value in box.xyxy[0].tolist()]
            detections.append(
                {
                    "class": names[int(box.cls[0].item())],
                    "confidence": round(confidence, 4),
                    "bbox": coords,
                }
            )
    return detections


def _plate_candidates(text: str) -> list[str]:
    candidates = []
    for line in text.splitlines():
        candidate = re.sub(r"[^A-Z0-9]", "", line.upper())
        if (
            5 <= len(candidate) <= 12
            and any(character.isalpha() for character in candidate)
            and any(character.isdigit() for character in candidate)
            and candidate not in candidates
        ):
            candidates.append(candidate)
    return candidates


def extract_text(data: bytes) -> dict:
    image = validate_image(data)
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("OCR is unavailable: install pytesseract and the Tesseract executable.") from exc
    _configure_tesseract(pytesseract)
    result = pytesseract.image_to_data(
        image,
        output_type=pytesseract.Output.DICT,
        lang=settings.ocr_language,
    )
    lines = {}
    confidences = []
    for index, (word_text, confidence) in enumerate(zip(result["text"], result["conf"])):
        word = word_text.strip()
        try:
            numeric_confidence = float(confidence)
        except (TypeError, ValueError):
            numeric_confidence = -1
        if word:
            line_key = (
                result["block_num"][index],
                result["par_num"][index],
                result["line_num"][index],
            )
            lines.setdefault(line_key, []).append(word)
        if numeric_confidence >= 0:
            confidences.append(numeric_confidence)
    text = "\n".join(" ".join(words) for words in lines.values())
    average_confidence = round(sum(confidences) / len(confidences)) if confidences else None
    warning = "Verify extracted text against the original image."
    if average_confidence is not None and average_confidence < 60:
        warning = "Confidence is low. Verify the text against the original image."
    return {
        "text": text,
        "plate_candidates": _plate_candidates(text),
        "confidence": average_confidence,
        "warning": warning,
    }


def service_state() -> dict[str, str]:
    state = {
        "yolo": "unavailable",
        "ocr": "unavailable",
        "notifications": "disabled",
        "maps": "client-side",
    }
    try:
        if settings.yolo_model_path:
            _yolo_model()
            state["yolo"] = "healthy"
    except Exception:
        state["yolo"] = "unavailable"
    try:
        import pytesseract

        _configure_tesseract(pytesseract)
        pytesseract.get_tesseract_version()
        state["ocr"] = "healthy"
    except Exception:
        state["ocr"] = "unavailable"
    return state
