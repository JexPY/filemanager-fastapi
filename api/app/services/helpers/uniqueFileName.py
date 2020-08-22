from uuid import uuid4


def generate_unique_name(extension, desiredExtension = False):
    unique = uuid4().hex
    # First goes original, second is thumbnail with desiredExtension
    return unique + '.' +  extension, unique + '.' +  desiredExtension if desiredExtension else desiredExtension