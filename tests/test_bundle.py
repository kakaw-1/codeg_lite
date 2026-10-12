import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'create_modelscope_bundle.py'
spec = importlib.util.spec_from_file_location('create_modelscope_bundle', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ModelScopeBundleTests(unittest.TestCase):
    def test_creates_pinned_dockerfile_and_sdk_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'modelscope'
            image = 'ghcr.io/example/codeg_lite:sha-0123456789ab'
            module.generate(image, output)
            dockerfile = (output / 'Dockerfile').read_text()
            readme = (output / 'README.md').read_text()
            self.assertIn(f'FROM {image}', dockerfile)
            self.assertIn('sdk: docker', readme)
            self.assertNotIn('CODEG_TOKEN=', dockerfile)

    def test_rejects_injected_image(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                module.generate('ghcr.io/org/repo:latest\nRUN echo hello', Path(directory))

    def test_rejects_unscoped_image(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                module.generate('codeg:latest', Path(directory))

if __name__ == '__main__':
    unittest.main()
