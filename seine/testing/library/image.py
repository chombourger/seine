# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Wraps the very spec 'test:' came from and the image seine.build/
# seine.inspect already know how to make and read -- 'create/build/
# deploy an artifact' from the prompt's own wishlist, without a second
# build engine: this calls straight into BuildCmd/Inspector, the same
# classes 'seine build'/'seine inspect' use.

from robot.api.deco import keyword, library

def _lookup(spec, path):
    node = spec
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError("'%s' has no '%s' in the active spec" % (path, part))
        node = node[part]
    return node

@library
class ImageLibrary:
    def __init__(self, context):
        self.context = context

    @keyword("Get Spec Value")
    def get_spec_value(self, path):
        """Reads 'path' (dotted, e.g. 'distribution.architecture') off the spec 'test:' came from."""
        if self.context.spec is None:
            raise RuntimeError("no specification is active for this run")
        return _lookup(self.context.spec, path)

    @keyword("Build Image")
    def build_image(self, *files):
        """Builds the image the spec 'test:' came from describes (or FILES, if given) describes."""
        from seine.build import BuildCmd
        build = BuildCmd()
        build.options = dict(build.options, ansible_library=[])
        build.load_all(list(files) or self.context.spec_files)
        spec = build.parse()
        build.build()
        self.context.spec = spec
        self.context.built_image = build
        return build.image._output

    @keyword("Inspect Image Path")
    def inspect_image_path(self, path="/"):
        """Lists 'path' inside the last built image, or reads it if it names a file."""
        build = getattr(self.context, "built_image", None)
        if build is None:
            raise RuntimeError("no image built yet this run -- call 'Build Image' first")
        from seine.inspect import Inspector
        with Inspector(build.raw_spec, build.image._output) as inspector:
            if inspector.is_dir(path):
                return [name for name, _kind, _size, _target in inspector.ls(path)]
            return inspector.cat(path).decode("utf-8", "replace")
