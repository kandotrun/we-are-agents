import hashlib
import io
import tarfile
import unittest

from waa.bootstrap import checked_archive


def archive(name="reason-network", *, link=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        member = tarfile.TarInfo(name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = "/bin/sh"
            tar.addfile(member)
        else:
            member.size = 6
            tar.addfile(member, io.BytesIO(b"binary"))
    return output.getvalue()


class DownloadChecks(unittest.TestCase):
    def test_bad_checksum_never_returns_executable(self):
        with self.assertRaisesRegex(RuntimeError, "Checksum mismatch"):
            checked_archive(archive(), "0" * 64, "reason-network")

    def test_symlink_is_not_an_executable_payload(self):
        data = archive(link=True)
        with self.assertRaisesRegex(RuntimeError, "regular"):
            checked_archive(data, hashlib.sha256(data).hexdigest(), "reason-network")

    def test_extracts_bytes_without_using_archive_path(self):
        data = archive("../../reason-network")
        self.assertEqual(checked_archive(data, hashlib.sha256(data).hexdigest(), "reason-network"), b"binary")
