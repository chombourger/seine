# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Two distinct kinds of observation, both plain values a test can
# 'assign:' and use in any 'if:'/'while:' condition or BuiltIn assertion
# -- no separate "observation" object to learn:
#
#  * 'Capture Screen' -- the console's decoded text (pyte's own screen,
#    the same one the Remote Target screen renders), for a target whose
#    state shows up as text: a boot log, a BIOS menu, a shell prompt.
#  * 'Capture Screen Image' -- a real frame off mtda's video RPC
#    (VideoSnapshot), for a target with nothing useful on its serial
#    console: a Wayland/Qt app has pixels, not text, to check.
#
# 'Screen Should Contain'/'Screen Matches' are convenience over the text
# case only, calling straight into BuiltIn (robot.libraries.BuiltIn)
# rather than reimplementing string/regexp matching. 'Classify Screen' is
# the seam for the image case's own missing half: an OpenCV/vision-model
# comparison over a captured frame. Capturing the frame is implemented
# (mtda's VideoSnapshot); classifying it is not -- this integration adds
# no OpenCV/vision-model dependency, seine has never needed one before
# now and the prompt only asks that the seam exist. Raising
# NotImplementedError here, rather than omitting the keyword, is
# deliberate: a test author can write 'Classify Screen' today and get a
# clear reason it doesn't run yet instead of 'no keyword with that name'.

import os
import time

from robot.api.deco import keyword, library

@library
class ObservationLibrary:
    def __init__(self, context):
        self.context = context
        self.captures = {}
        self.images = {}

    def _console(self):
        console = getattr(self.context, "_target_console", None)
        if console is None:
            raise RuntimeError(
                "no live console -- 'Connect Target' first (and the mtda "
                "agent must expose a remote console)")
        return console

    def _artifact_path(self, name, suffix):
        path = os.path.join(self.context.outdir or ".",
                            "%s-%d.%s" % (name, int(time.time()), suffix))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return path

    @keyword("Capture Screen")
    def capture_screen(self, name=None):
        """Returns the console's current screen as text, and saves it under 'name' if given."""
        text = "\n".join(self._console().screen.display)
        if name:
            self.captures[name] = text
            if self.context.outdir:
                with open(self._artifact_path(name, "txt"), "w") as f:
                    f.write(text)
        return text

    @keyword("Screen Should Contain")
    def screen_should_contain(self, expected, name=None):
        """Fails unless the current screen (or the capture 'name' names) contains 'expected'."""
        from robot.libraries.BuiltIn import BuiltIn
        text = self.captures[name] if name else self.capture_screen()
        BuiltIn().should_contain(text, expected)

    @keyword("Screen Matches")
    def screen_matches(self, pattern, name=None):
        """Fails unless the current screen (or the capture 'name' names) matches regexp 'pattern'."""
        from robot.libraries.BuiltIn import BuiltIn
        text = self.captures[name] if name else self.capture_screen()
        BuiltIn().should_match_regexp(text, pattern)

    # 'name' with no 'outdir' still returns the path of a temp file --
    # a captured frame is not text a test can 'Should Contain' its way
    # through, so the file is the value, not a fallback for one.
    @keyword("Capture Screen Image")
    def capture_screen_image(self, name="screen"):
        """Saves a real video frame (mtda's VideoSnapshot) and returns its file path."""
        from seine.tui import target
        data, content_type = target.video_snapshot(self.context)
        if not data:
            raise RuntimeError(
                "target has no video source configured (VideoSnapshot "
                "returned nothing) -- Capture Screen Image needs mtda's "
                "own video support, unlike the console-based Capture Screen")
        suffix = (content_type or "image/jpeg").rsplit("/", 1)[-1]
        if not self.context.outdir:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="%s-" % name, suffix=".%s" % suffix)
            os.close(fd)
        else:
            path = self._artifact_path(name, suffix)
        with open(path, "wb") as f:
            f.write(data)
        self.images[name] = path
        return path

    @keyword("Classify Screen")
    def classify_screen(self, name=None, model=None):
        """Not implemented: the seam for an OpenCV/vision classifier over a captured image -- see this file's own comment."""
        raise NotImplementedError(
            "no OpenCV/vision-model classifier is wired in yet -- "
            "'Capture Screen Image' already gives you a real frame to "
            "feed one once this grows; see "
            "seine/testing/library/observation.py")
