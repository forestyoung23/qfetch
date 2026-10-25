import hashlib
from pathlib import Path
import tempfile
import unittest

from verify_torrent_file import verify


def encode(v):
    if isinstance(v, int): return b'i' + str(v).encode() + b'e'
    if isinstance(v, bytes): return str(len(v)).encode() + b':' + v
    if isinstance(v, list): return b'l' + b''.join(encode(x) for x in v) + b'e'
    return b'd' + b''.join(encode(k) + encode(v[k]) for k in sorted(v)) + b'e'


class Pieces(unittest.TestCase):
    def test_multifile_boundary_is_reported_not_claimed_verified(self):
        content = b'abcdefgh' + b'ij'  # selected file ends inside the third piece
        whole = content + b'kl'
        pieces = b''.join(hashlib.sha1(whole[i:i+4]).digest() for i in range(0, len(whole), 4))
        raw = encode({b'info': {b'name': b'film', b'piece length': 4, b'pieces': pieces,
            b'files': [{b'length': 10, b'path': [b'film.mp4']}, {b'length': 2, b'path': [b'ad.doc']}]}})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'film.mp4'; p.write_bytes(content)
            result = verify(raw, 'film/film.mp4', p)
            self.assertEqual(result['verified_pieces'], 2)
            self.assertEqual(result['unverified_boundary_bytes'], 2)
            self.assertEqual(result['unverified_boundary_piece_indices'], [2])
            p.write_bytes(b'00000000ij')  # sparse/unwritten data has the correct length
            with self.assertRaises(ValueError): verify(raw, 'film/film.mp4', p)

    def test_single_file_last_short_piece_is_verified(self):
        content = b'abcde'
        pieces = hashlib.sha1(b'abcd').digest() + hashlib.sha1(b'e').digest()
        raw = encode({b'info': {b'name': b'film.mp4', b'length': 5, b'piece length': 4, b'pieces': pieces}})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'film.mp4'; p.write_bytes(content)
            result = verify(raw, 'film.mp4', p)
            self.assertEqual(result['verified_bytes'], 5)
            self.assertEqual(result['unverified_boundary_bytes'], 0)

    def test_short_or_malformed_pieces_never_pass(self):
        raw = encode({b'info': {b'name': b'film.mp4', b'length': 5, b'piece length': 4, b'pieces': b'bad'}})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'film.mp4'; p.write_bytes(b'abcde')
            with self.assertRaises(ValueError): verify(raw, 'film.mp4', p)


if __name__ == '__main__': unittest.main()
