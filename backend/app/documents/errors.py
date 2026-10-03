class DocumentParseError(Exception):
    pass


class DocumentTooLarge(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class DocumentConflict(Exception):
    pass


class DocumentValidationError(Exception):
    pass
