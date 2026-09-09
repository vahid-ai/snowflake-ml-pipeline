"""Local Iceberg FileIO with dlt file-URI decoding on Windows and POSIX."""

from urllib.parse import unquote

from pyiceberg.io import InputFile, OutputFile
from pyiceberg.io.fsspec import FsspecFileIO, FsspecInputFile, FsspecOutputFile


class LocalFileIO(FsspecFileIO):
    """Decode URI escapes before fsspec treats them as literal path characters.

    dlt emits percent-encoded file URIs, including its Windows file://C:/ form.
    fsspec accepts both Windows forms but does not itself decode %20. Keep this
    adapter local-only so remote S3 keys are never inadvertently decoded.
    """

    @staticmethod
    def _path(location: str) -> str:
        if not location.startswith("file://"):
            raise ValueError(f"Local Iceberg storage requires a file URI: {location!r}")
        return unquote(location)

    def new_input(self, location: str) -> FsspecInputFile:
        return super().new_input(self._path(location))

    def new_output(self, location: str) -> FsspecOutputFile:
        return super().new_output(self._path(location))

    def delete(self, location: str | InputFile | OutputFile) -> None:
        # File objects returned above already contain decoded paths.
        super().delete(self._path(location) if isinstance(location, str) else location)
