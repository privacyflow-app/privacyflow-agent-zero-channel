"""
PrivacyFlow Channel — plugin hooks.

A0 invokes these via helpers/plugins.py:call_plugin_hook on config load/save.
Hooks live at the plugin root as hooks.py (HOOKS_SCRIPT in helpers/plugins.py).
"""

from helpers import plugins as _plugins
from helpers.print_style import PrintStyle

PLUGIN_NAME = "privacyflow_channel"


def save_plugin_config(settings=None, **kwargs):
    """Refresh plugin extensions when config is saved so the poller starts
    without a container restart.

    The PrivacyFlow poller is a job_loop extension. A0 caches the loaded
    job_loop extensions and only refreshes that cache on a plugin change
    (toggle/install) or at boot — config-only saves deliberately skip the
    reload (see the commented-out after_plugin_change in
    helpers/plugins.py:save_plugin_config). As a result, installing and
    configuring this plugin into an already-running instance left the poller
    unloaded until a restart.

    Calling after_plugin_change here refreshes the extension cache, so the
    next job_loop tick (<=60s) loads the poller — no restart needed. Toggle
    on/off is already handled by A0 core (toggle_plugin calls
    after_plugin_change), so this specifically covers the configure-and-save
    path, which is the natural last step after installing the plugin.
    """
    try:
        _plugins.after_plugin_change([PLUGIN_NAME])
    except Exception as e:
        PrintStyle.error(f"[pf_channel] Failed to reload extensions after config save: {e}")
    return settings
