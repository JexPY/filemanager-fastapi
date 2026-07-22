from typing import Tuple
from uuid import uuid4


def generate_unique_name(extension: str, desiredExtension: bool = False) -> Tuple:
    unique = uuid4().hex
    # First goes original, second is thumbnail with desiredExtension
    return (unique + '.' + extension, unique + '.' + desiredExtension if desiredExtension else desiredExtension)
