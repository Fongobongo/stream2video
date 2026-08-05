"""WaveformMixin — waveform popup (preview + zoom/pan + render + poll).

Historically a single 1050-line module. The class is decomposed into
three role-specific mixins; this module is the public entry point and
combines them into ``WaveformMixin``, which ``gui.py`` mixes into the
main window class along with the other GUI mixins.
"""

from stream2video.gui_waveform_interactions import WaveformInteractionsMixin
from stream2video.gui_waveform_render import WaveformRenderMixin
from stream2video.gui_waveform_window import WaveformWindowMixin


class WaveformMixin(WaveformWindowMixin, WaveformInteractionsMixin, WaveformRenderMixin):
    """Composed waveform popup mixin (window + interactions + render)."""

    __slots__ = ()
