import pydub

from pydub import effects
from gtts import gTTS
from io import BytesIO


def get_speak_file(message_content, lang):
    pre_processed = BytesIO()
    post_processed = BytesIO()
    print(1)
    spoken_google = gTTS(message_content, lang=lang)
    spoken_google.write_to_fp(fp=pre_processed)
    print(2)
    segment = pydub.AudioSegment.from_file(pre_processedformat="mp3")
    print(3)
    segment = effects.speedup(segment, 1.25, 150, 25)
    print(4)
    segment.set_frame_rate(16000).export(post_processed, format="wav")
    print(5)
    return post_processed
