import unittest

from forum_source import parse_page

URL = 'https://www.1lou.me/thread-123.htm'
HTML = '''<h4>Movie</h4><a href="attach-download-99.htm">outside ad</a>
<div class="message break-all" isfirst="1"><div><a href="https://www.imdb.com/title/tt29319990/">IMDb</a></div>
<a href="attach-download-42.htm">real attachment</a>
<a href="https://other.example/attach-download-43.htm">external ad</a></div>
<div class="message" isfirst="0"><a href="https://www.imdb.com/title/tt0000000/">reply</a></div>'''


class Forum(unittest.TestCase):
    def test_plain_text_imdb_link_used_by_live_forum(self):
        text = HTML.replace('<a href="https://www.imdb.com/title/tt29319990/">IMDb</a>', '◎IMDb链接　https://www.imdb.com/title/tt29319990/')
        self.assertEqual(parse_page(URL, text, 'tt29319990')['imdb_id'], 'tt29319990')

    def test_only_first_post_identity_and_same_host_attachments(self):
        result = parse_page(URL, HTML, 'tt29319990')
        self.assertEqual(result['attachment_urls'], ['https://www.1lou.me/attach-download-42.htm'])

    def test_other_film_or_login_page_cannot_generate_candidates(self):
        for text in [HTML.replace('tt29319990', 'tt1234567'), '<html>Please login</html>']:
            with self.assertRaises(ValueError): parse_page(URL, text, 'tt29319990')

    def test_other_domain_and_ambiguous_main_post_rejected(self):
        with self.assertRaises(ValueError): parse_page('https://evil.example/thread-123.htm', HTML, 'tt29319990')
        extra = HTML.replace('real attachment</a>', 'real attachment</a><a href="https://www.imdb.com/title/tt1234567/">Other movie</a>')
        with self.assertRaises(ValueError): parse_page(URL, extra, 'tt29319990')


if __name__ == '__main__': unittest.main()
