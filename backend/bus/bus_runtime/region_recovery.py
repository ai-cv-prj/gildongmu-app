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
    'same_number_min_score': .98,
    'same_number_symmetric_margins': (.15, .20),
    'same_number_vertical_margin': .10,
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


def is_same_number(source, text, scores):
    """Only a density-rejected full numeric read can seed this exception."""
    original = source['text']
    minimum = RECOVERY_POLICY['same_number_min_score']
    return (source.get('rejection_reason') == 'implausible_character_density'
            and not source.get('eligible')
            and not source.get('visibility_rejection_reason')
            and re.fullmatch(r'[0-9]{3,4}', original) is not None
            and len(source.get('token_scores', ())) >= len(original)+1
            and token_quality(source.get('token_scores')) >= minimum
            and text == original and len(scores) >= len(text)+1
            and token_quality(scores) >= minimum)


def same_number_confirmation_box(box, side, source_box, search_box):
    """Verify both word ends with a fixed small margin around the original ROI."""
    if side not in {'left', 'right'}:
        return None
    a,b,c,d = box
    x,y,z,w = search_box
    sa,sb,sc,sd = source_box
    height = sd-sb
    horizontal = max(2, round(height*RECOVERY_POLICY['same_number_symmetric_margins'][1]))
    vertical = max(2, round(height*RECOVERY_POLICY['same_number_vertical_margin']))
    confirm = (sa-horizontal, sb-vertical, sc+horizontal, sd+vertical)
    if not (a <= sa < sc <= c and b <= sb < sd <= d
            and (a < sa if side=='left' else c > sc)
            and confirm[0] < sa and confirm[2] > sc
            and confirm != tuple(box)
            and (confirm[2]-confirm[0], confirm[3]-confirm[1]) != (c-a, d-b)
            and confirm[0] > x+1 and confirm[1] > y+1
            and confirm[2] < z-1 and confirm[3] < w-1):
        return None
    return confirm


def same_number_fallback_boxes(source, shape):
    """Two fixed small margins, only for a high-score density-rejected number."""
    if not is_same_number(source, source['text'], source.get('token_scores', ())):
        return []
    a,b,c,d = source['text_box']
    x,y,z,w = source['search_box']
    image_height, image_width = shape[:2]
    height = d-b
    if c <= a or height <= 0:
        return []
    vertical = max(2, round(height*RECOVERY_POLICY['same_number_vertical_margin']))
    boxes=[]
    for fraction in RECOVERY_POLICY['same_number_symmetric_margins']:
        horizontal = max(2, round(height*fraction))
        box = (a-horizontal, b-vertical, c+horizontal, d+vertical)
        if not (box[0] > max(x, 0)+1 and box[1] > max(y, 0)+1
                and box[2] < min(z, image_width)-1
                and box[3] < min(w, image_height)-1):
            return []
        if ((box[2]-box[0])/(box[3]-box[1])/len(source['text'])
                < RECOVERY_POLICY['min_extended_aspect_per_character']):
            return []
        boxes.append(box)
    # Re-reading identical pixels must not masquerade as two margin checks.
    return boxes if len(set(boxes)) == 2 else []
