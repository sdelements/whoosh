# Copyright 2012 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.


# Exceptions

class NoStemmer(Exception):
    pass


class NoStopWords(Exception):
    pass


# Data and functions for language names

languages = ("ar", "da", "nl", "en", "fi", "fr", "de", "hu", "it", "no", "pt",
             "ro", "ru", "es", "sv", "tr")

aliases = {
           # By ISO 639-1 three letter codes
           "ara": "ar",
           "dan": "da", "nld": "nl", "eng": "en", "fin": "fi", "fra": "fr",
           "deu": "de", "hun": "hu", "ita": "it", "nor": "no", "por": "pt",
           "ron": "ro", "rus": "ru", "spa": "es", "swe": "sv", "tur": "tr",

           # By name in English
           "arabic": "ar",
           "danish": "da",
           "dutch": "nl",
           "english": "en",
           "finnish": "fi",
           "french": "fr",
           "german": "de",
           "hungarian": "hu",
           "italian": "it",
           "norwegian": "no",
           "portuguese": "pt",
           "romanian": "ro",
           "russian": "ru",
           "spanish": "es",
           "swedish": "sw",
           "turkish": "tr",

           # By name in own language
           "العربية": "ar",
           "dansk": "da",
           "nederlands": "nl",
           "suomi": "fi",
           "français": "fr",
           "deutsch": "de",
           "magyar": "hu",
           "italiano": "it",
           "norsk": "no",
           "português": "pt",
           "русский язык": "ru",
           "español": "es",
           "svenska": "sv",
           "türkçe": "tr",
           }


def two_letter_code(name):
    if name in languages:
        return name
    if name in aliases:
        return aliases[name]
    return None


# Getter functions

def stemmer_for_language(lang):
    if lang == "porter":
        from .porter import stem as porter_stem
        return porter_stem
    elif lang == "porter2":
        from .porter2 import stem as porter2_stem
        return porter2_stem

    tlc = two_letter_code(lang)

    if tlc == "ar":
        from .isri import ISRIStemmer
        return ISRIStemmer().stem

    from .snowball import classes as snowball_classes
    if tlc in snowball_classes:
        return snowball_classes[tlc]().stem

    raise Exception("No stemmer available for %r" % lang)


def stopwords_for_language(lang):
    from .stopwords import stoplists

    tlc = two_letter_code(lang)
    if tlc in stoplists:
        return stoplists[tlc]

    raise Exception("No stop-word list available for %r" % lang)



