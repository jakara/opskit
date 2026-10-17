"""交付文档的真实 HTTP 读取、安全边界与离线前端资源。"""
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zipfile

from test_installer import ROOT, module

workbench = module('workbench_documents', ROOT / 'management/portal/server.py')


def word_file(extra=None):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', '<document><body>现场手册</body></document>')
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return content.getvalue()


class WorkbenchDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.documents = self.root / 'documents'
        self.documents.mkdir()
        self.server = workbench.WorkbenchServer(('127.0.0.1', 0), self.root / 'catalog',
                                               seed_tools=True, documents_dir=self.documents)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def file(self, path, data):
        target = self.documents / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
        return target

    def request(self, path):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('GET', path)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def content(self, path, download=False):
        return self.request('/hub/api/documents/content?path=' + quote(path, safe='') + ('&download=1' if download else ''))

    def test_list_grouping_chinese_paths_and_original_download(self):
        text = '# 现场安装\n\n```sh\n./install.sh\n```\n'
        self.file('delivery/school/现场 安装.md', text)
        self.file('project/业务说明.txt', '数据库说明')
        self.file('builtin/OpsKit.md', '内置文档')
        self.file('散落说明.markdown', '本地文件')
        self.file('.env', 'SECRET=never-serve')
        self.file('.private/保密.md', 'never-list')
        self.file('project/private.json', '{}')
        code, _, body = self.request('/hub/api/documents')
        self.assertEqual(code, 200)
        entries = json.loads(body)['documents']
        self.assertEqual([entry['group'] for entry in entries], ['交付手册', '项目文档', '内置说明', '本地文档'])
        self.assertEqual(entries[0], {'path': 'delivery/school/现场 安装.md', 'title': '现场安装', 'group': '交付手册', 'kind': 'markdown', 'size': len(text.encode())})
        code, headers, body = self.content(entries[0]['path'], download=True)
        self.assertEqual((code, body.decode()), (200, text))
        self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertIn("filename*=UTF-8''" + quote('现场 安装.md', safe=''), headers['Content-Disposition'])
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_rejects_traversal_symlinks_hidden_files_and_special_files(self):
        outside = self.root / 'outside.txt'
        outside.write_text('secret')
        self.file('normal.md', 'safe')
        (self.documents / 'linked.txt').symlink_to(outside)
        (self.documents / 'linked-folder').symlink_to(self.root, target_is_directory=True)
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.documents / 'pipe.txt')
        for name in ('../outside.txt', '/outside.txt', 'a/../../outside.txt', 'a\\..\\outside.txt', '.env', '.private/a.md', 'linked.txt', 'linked-folder/outside.txt', 'pipe.txt', 'normal.md\x00', 'normal.md\n'):
            with self.subTest(name=name):
                self.assertIn(self.content(name)[0], (400, 404))
        self.assertEqual([entry['path'] for entry in json.loads(self.request('/hub/api/documents')[2])['documents']], ['normal.md'])
        self.assertEqual(self.request('/hub/api/documents/content')[0], 400)
        self.assertEqual(self.request('/hub/api/documents/content?path=normal.md&path=normal.md')[0], 400)
        self.assertEqual(self.content('missing.md')[0], 404)

    def test_oversized_files_and_unsafe_word_zip_are_not_delivered(self):
        path = self.file('huge.txt', '')
        with path.open('wb') as stream:
            stream.truncate(workbench.MAX_DOCUMENT_SIZE + 1)
        self.assertEqual(self.content('huge.txt')[0], 400)
        self.assertFalse(json.loads(self.request('/hub/api/documents')[2])['documents'])
        for name, data in (
                ('broken.docx', b'not a ZIP'),
                ('traversal.docx', word_file({'../outside.txt': b'unsafe'})),
                ('bomb.docx', word_file({'word/media/huge.bin': b'A' * (2 * 1024 * 1024)}))):
            with self.subTest(name=name):
                self.file(name, data)
                code, _, response = self.content(name)
                self.assertEqual(code, 400)
                self.assertIn('Word', json.loads(response)['error'])
        self.file('valid.docx', word_file())
        self.assertEqual(self.content('valid.docx')[0], 200)

    def test_document_mimes_script_isolation_and_legacy_word(self):
        samples = {'guide.pdf': (b'%PDF-1.4\n', 'application/pdf', 'pdf'),
                   'guide.docx': (word_file(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'word'),
                   'old.doc': (b'legacy Word', 'application/msword', 'legacy-word'),
                   'icon.svg': (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 'image/svg+xml', 'image'),
                   'picture.PNG': (b'PNG', 'image/png', 'image')}
        for name, (data, mime, _) in samples.items():
            self.file(name, data)
            code, headers, body = self.content(name)
            self.assertEqual((code, body, headers['Content-Type']), (200, data, mime))
            self.assertIn("script-src 'none'", headers['Content-Security-Policy'])
            self.assertIn('sandbox;', headers['Content-Security-Policy'])
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        kinds = {entry['path']: entry['kind'] for entry in json.loads(self.request('/hub/api/documents')[2])['documents']}
        self.assertEqual(kinds, {name: values[2] for name, values in samples.items()})

    def test_offline_frontend_and_existing_workbench_routes_remain_available(self):
        for path in ('/', '/hub/docs', '/hub/docs/?path=delivery%2Fguide.md', '/hub/app.js', '/hub/documents.js', '/hub/style.css',
                     '/hub/vendor/marked-18.1.1.umd.js', '/hub/vendor/purify-3.4.17.min.js', '/hub/vendor/mammoth-1.13.0.browser.min.js',
                     '/hub/vendor/pdfjs-6.4.299/build/pdf.mjs', '/hub/vendor/pdfjs-6.4.299/build/pdf.worker.mjs',
                     '/hub/vendor/pdfjs-6.4.299/cmaps/UniGB-UCS2-H.bcmap',
                     '/hub/vendor/pdfjs-6.4.299/standard_fonts/LiberationSans-Regular.ttf'):
            self.assertEqual(self.request(path)[0], 200, path)
        self.assertIn('javascript', self.request('/hub/vendor/pdfjs-6.4.299/build/pdf.worker.mjs')[1]['Content-Type'])
        _, headers, page = self.request('/')
        self.assertIn('交付文档'.encode(), page)
        self.assertNotIn(b'https://cdn', page)
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertIn("worker-src 'self'", headers['Content-Security-Policy'])
        self.assertIn("frame-src 'none'", headers['Content-Security-Policy'])
        self.assertIn("object-src 'none'", headers['Content-Security-Policy'])
        for path in ('/hub/server.py', '/hub/vendor/../server.py', '/hub/vendor/README.md',
                     '/hub/vendor/pdfjs-6.4.299/wasm/quickjs-eval.js',
                     '/hub/vendor/pdfjs-6.4.299/cmaps/../../../server.py'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/hub/')[0], 302)
        with patch.object(self.server.discovery, 'read', return_value={'containers': [], 'http_services': []}):
            code, _, state = self.request('/hub/api/state')
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(state)['entries']), 4)

    def test_default_directory_is_optional_and_old_constructor_calls_work(self):
        with patch.object(workbench, 'ASSETS', self.root / 'portal'):
            with workbench.WorkbenchServer(('127.0.0.1', 0), self.root / 'old-catalog') as server:
                self.assertEqual(server.documents.directory, self.documents)
            with workbench.WorkbenchServer(('127.0.0.1', 0), self.root / 'empty-catalog', documents_dir=self.root / 'absent') as server:
                self.assertEqual(server.documents.listing(), [])
                self.assertFalse((self.root / 'absent').exists())
        with patch.object(self.server.documents, 'directory', self.documents / 'symlink'):
            (self.documents / 'symlink').symlink_to(self.documents, target_is_directory=True)
            self.assertEqual(self.request('/hub/api/documents')[0], 500)
            self.assertEqual(self.content('normal.md')[0], 400)


if __name__ == '__main__':
    unittest.main()
