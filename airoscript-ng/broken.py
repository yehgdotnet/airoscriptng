ESSIDS = [
    'WLAN_',
    'JAZZTEL_'
] # Those have more probability to be hacked via dicts.

PRIVACY = {
    'WPA': [50, ['get_wpa_handshake', 'pyrit']],
    'WPA2': [10, ['get_wpa_handshake',  'pyrit']],
    'WPA2WPA': [10, ['get_wpa_handshake', 'pyrit']],
    'WEP': [900, ['wep_dissasication', 'wep_caffe_latte', 'wep_fragmentation', 'wep_chopchop' ]]
}

def get_hackability_name(point):
    hackabilities = {
        20: 'VERY LOW',
        50: 'LOW',
        60: 'NORMAL',
        70: 'GOOD',
        90: 'VERY GOOD',
        100: 'BEST OPTIONS'
    }
    for hackability in hackabilities.keys():
        if point > hackability:
            return hackabilities[hackability]
    else:
        return 'VERY LOW'


