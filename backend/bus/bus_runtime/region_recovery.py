"""Re-read pixels beside numeric regions; never complete strings from a target."""
import re
from .target import token_quality

RECOVERY_POLICY = {
    'source_min_score': .9,
    'extension_min_score': .98,
    'left_height': .5,
    'right_height': 1.5,
    'confirmation_extra_height': .15,
    'right_confirmation_left_margin_height': .5,
    'left_extension': 'supported alphabetic prefix only; adjacent digits may be unrelated text',
    'max_sources_per_bus': 4,
    'min_extended_aspect_per_character': .20,
    'note': 'two crop margins must agree on the same frame; original digits must be preserved',
}


def recovery_boxes(p, shape):
    if (p['rejection_reason'] not in {'', 'implausible_character_density'}
            or not re.fullmatch(r'[0-9]{2,4}', p['text'])
            or token_quality(p['token_scores']) < .9):
        return []
    a,b,c,d = p['text_box']
    x,y,z,w = p['search_box']
    height = d-b
    pad = max(2, round(height*.10))
    result = []
    for side, amount in [('left', .5), ('right', 1.5)]:
        # Do not promote a crop that crosses the searched bus region or image edge.
        box = (a-round(amount*height) if side=='left' else a,
               b-pad, c+round(amount*height) if side=='right' else c, d+pad)
        if box[0]>x+1 and box[1]>y+1 and box[2]<z-1 and box[3]<w-1:
            result.append((side, box))
    return result


def confirmation_box(box, side, source_box, search_box):
    a,b,c,d=box; x,y,z,w=search_box
    extra=max(2,round((source_box[3]-source_box[1])*.15))
    opposite=max(2,round((source_box[3]-source_box[1])*.5))
    confirm=(a-extra if side=='left' else a-opposite,b,c+extra if side=='right' else c,d)
    return confirm if confirm[0]>x+1 and confirm[2]<z-1 else None


def is_extension(source, text, scores, side):
    if len(scores)<len(text)+1 or token_quality(scores)<.98:
        return False
    if not re.fullmatch(r'(?:M|N)?[0-9]{2,4}[AB]?',text):
        return False
    if len(text)<=len(source):
        return False
    return text in {'M'+source,'N'+source} if side=='left' else text.startswith(source)
