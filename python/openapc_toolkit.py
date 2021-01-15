#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import csv
from collections import OrderedDict
import json
import locale
import logging
from logging.handlers import MemoryHandler
import os
import re
from shutil import copyfileobj
import sys
from urllib.request import build_opener, urlopen, urlretrieve, HTTPErrorProcessor, Request
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

import mappings

try:
    import chardet
except ImportError:
    chardet = None
    print("WARNING: 3rd party module 'chardet' not found - character " +
          "encoding guessing will not work")

# Identifying User Agent header for metadata API requests
USER_AGENT = ("OpenAPC Toolkit (https://github.com/OpenAPC/openapc-de/blob/master/python/openapc_toolkit.py;"+
              " mailto:openapc@uni-bielefeld.de)")

# regex for detecing DOIs
DOI_RE = re.compile(r"^(((https?://)?(dx.)?doi.org/)|(doi:))?(?P<doi>10\.[0-9]+(\.[0-9]+)*\/\S+)", re.IGNORECASE)
# regex for detecting shortDOIs
SHORTDOI_RE = re.compile(r"^(https?://)?(dx.)?doi.org/(?P<shortdoi>[a-z0-9]+)$", re.IGNORECASE)

ISSN_RE = re.compile(r"^(?P<first_part>\d{4})\-(?P<second_part>\d{3})(?P<check_digit>[\dxX])$")

OAI_COLLECTION_CONTENT = OrderedDict([
    ("institution", "intact:institution"),
    ("period", "intact:period"),
    ("euro", "intact:euro"),
    ("doi", "intact:id_number[@type='doi']"),
    ("is_hybrid", "intact:is_hybrid"),
    ("publisher", "intact:publisher"),
    ("journal_full_title", "intact:journal_full_title"),
    ("issn", "intact:issn"),
    ("license_ref", "intact:licence"),
    ("pmid", "intact:id_number[@type='pubmed']"),
    ("url", None),
    ("local_id", "intact:id_number[@type='local']")
])

MESSAGES = {
    "num_columns": "Syntax: The number of values in this row (%s) " +
                   "differs from the number of columns (%s). Line left " +
                   "unchanged, the resulting CSV file will not be valid.",
    "locale": "Error: Could not process the monetary value '%s' in " +
              "column %s. Usually this happens due to one of two reasons:\n1) " +
              "The value does not represent a number.\n2) The value " +
              "represents a number, but its format differs from your " +
              "current system locale - the most common source of error " +
              "is the decimal mark (1234.56 vs 1234,56). Try using " +
              "another locale with the -l option.",
    "unify": "Normalisation: Crossref-based {} changed from '{}' to '{}' " +
             "to maintain consistency.",
    "digits_error": "Monetary value %s has more than 2 digits after " +
                    "the decimal point. If this is just a formatting issue (from automated " +
                    "conversion for example) you may call the enrichment script with the -r " +
                    "option to round such values to 2 digits automatically.",
    "digits_norm": "Normalisation: Monetary value %s rounded to 2 digits after " +
                   "decimal mark (%s -> %s)",
    "doi_norm": "Normalisation: DOI '{}' normalised to pure form ({}).",
    "springer_distinction": "publisher 'Springer Nature' found " +
                            "for a pre-2015 article - publisher " +
                            "changed to '%s' based on prefix " +
                            "discrimination ('%s')",
    "unknown_prefix": "publisher 'Springer Nature' found for a " +
                      "pre-2015 article, but discrimination was " +
                      "not possible - unknown prefix ('%s')",
    "issn_hyphen_fix": "Normalisation: Added hyphen to %s value (%s -> %s)",
    "period_format": "Normalisation: Date format in period column changed to year only (%s -> %s)",
    "unknown_hybrid_identifier": "Unknown identifier in 'is_hybrid' column ('%s').",
    "hybrid_normalisation": "Normalisation: is_hybrid status changed from '%s' to '%s'.",
    "no_hybrid_identifier": "Empty value in 'is_hybrid' column."
}

# Do not quote the values in the 'period' and 'euro' columns
OPENAPC_STANDARD_QUOTEMASK = [
    True,
    False,
    False,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True,
    True
]

COLUMN_SCHEMAS = {
    "journal_article": [
        "institution",
        "period",
        "euro",
        "doi",
        "is_hybrid",
        "publisher",
        "journal_full_title",
        "issn",
        "issn_print",
        "issn_electronic",
        "issn_l",
        "license_ref",
        "indexed_in_crossref",
        "pmid",
        "pmcid",
        "ut",
        "url",
        "doaj"
    ],
    "journal_article_transagree": [
        "institution",
        "period",
        "euro",
        "doi",
        "is_hybrid",
        "publisher",
        "journal_full_title",
        "issn",
        "issn_print",
        "issn_electronic",
        "issn_l",
        "license_ref",
        "indexed_in_crossref",
        "pmid",
        "pmcid",
        "ut",
        "url",
        "doaj",
        "agreement"
    ],
    "book_title": [
        "institution",
        "period",
        "euro",
        "doi",
        "backlist_oa",
        "publisher",
        "book_title",
        "isbn",
        "isbn_print",
        "isbn_electronic",
        "license_ref",
        "indexed_in_crossref",
        "doab"
    ]
}

class OpenAPCUnicodeWriter(object):
    """
    A customized CSV Writer.

    A custom CSV writer. Encodes output in Unicode and can be configured to
    follow the open APC CSV quotation standards. A quote mask can also be
    provided to enable or disable value quotation in distinct CSV columns.

    Attributes:
        quotemask: A quotemask is a list of boolean values which should have
                   the same length as the number of columns in the csv file.
                   On writing, the truth values in the codemask will determine
                   if the values in the according column will be quoted. If no
                   quotemask is provided, every field will be quoted.
        openapc_quote_rules: Determines if the special openapc quote rules
                             should be applied, meaning that the keywords
                             NA, TRUE and FALSE will never be quoted. This
                             always takes precedence over a quotemask.
        has_header: Determines if the csv file has a header. If that's the case,
                    The values in the first row will all be quoted regardless
                    of any quotemask.
        minimal_quotes: Quote values containing a comma even if a quotemask
                        is False for that column (Might produce a malformed
                        csv file otherwise).
    """

    def __init__(self, f, quotemask=None, openapc_quote_rules=True,
                 has_header=True, minimal_quotes=True):
        self.outfile = f
        self.quotemask = quotemask
        self.openapc_quote_rules = openapc_quote_rules
        self.has_header = has_header
        self.minimal_quotes = minimal_quotes

    def _prepare_row(self, row, use_quotemask):
        for index in range(len(row)):
            if self.openapc_quote_rules and row[index] in ["TRUE", "FALSE", "NA"]:
                # Never quote these keywords
                continue
            if not use_quotemask or not self.quotemask:
                # Always quote without a quotemask
                row[index] = row[index].replace('"', '""')
                row[index] = '"' + row[index] + '"'
                continue
            if index < len(self.quotemask):
                if self.quotemask[index] or "," in row[index] and self.minimal_quotes:
                    row[index] = row[index].replace('"', '""')
                    row[index] = '"' + row[index] + '"'
        return row

    def _write_row(self, row):
        line = ",".join(row) + "\n"
        self.outfile.write(line)

    def write_rows(self, rows):
        if self.has_header:
            self._write_row(self._prepare_row(rows.pop(0), False))
        for row in rows:
            self._write_row(self._prepare_row(row, True))

class DOAJAnalysis(object):

    def __init__(self, doaj_csv_file, update=False):
        self.doaj_issn_map = {}
        self.doaj_eissn_map = {}
        
        if not os.path.isfile(doaj_csv_file) or update :
            doaj_csv_file = self.download_doaj_csv(doaj_csv_file)

        handle = open(doaj_csv_file, "r")
        reader = csv.DictReader(handle)
        for line in reader:
            journal_title = line["Journal title"]
            issn = line["Journal ISSN (print version)"]
            eissn = line["Journal EISSN (online version)"]
            if issn:
                self.doaj_issn_map[issn] = journal_title
            if eissn:
                self.doaj_eissn_map[eissn] = journal_title

    def lookup(self, any_issn):
        if any_issn in self.doaj_issn_map:
            return self.doaj_issn_map[any_issn]
        elif any_issn in self.doaj_eissn_map:
            return self.doaj_eissn_map[any_issn]
        return None
        
    def download_doaj_csv(self, filename):
        request = Request("https://doaj.org/csv")
        request.add_header("User-Agent", USER_AGENT)
        with urlopen(request) as source:
            with open(filename, "wb") as dest:
                copyfileobj(source, dest)
        return filename

class DOABAnalysis(object):

    def __init__(self, isbn_handling, doab_csv_file, update=False, verbose=False):
        self.isbn_map = {}
        self.isbn_handling = isbn_handling

        if not os.path.isfile(doab_csv_file) or update:
            self.download_doab_csv(doab_csv_file)

        lines = []
        # The file might contain NUL bytes, we need to get rid of them before
        # handing the lines to a DictReader
        with open(doab_csv_file, "r") as handle:
            for line in handle:
                if "\x00" in line:
                    continue
                lines.append(line)
        duplicate_isbns = []
        reader = csv.DictReader(lines)
        for line in reader:
            isbn_string = line["ISBN"]
            record_type = line["Type"]
            # ATM we focus on books only
            if record_type != "book":
                continue
            # may contain multi-values split by a whitespace, tab, slash or semicolon...
            isbn_string = isbn_string.replace("/", " ")
            isbn_string = isbn_string.replace(";", " ")
            isbn_string = isbn_string.replace("\t", " ")
            isbn_string = isbn_string.strip()
            if len(isbn_string) == 0:
                continue
            while "  " in isbn_string:
               isbn_string = isbn_string.replace("  ", " ")
            isbns = isbn_string.split(" ")
            # ...which may also contain duplicates
            for isbn in list(set(isbns)):
                result = self.isbn_handling.test_and_normalize_isbn(isbn)
                if not result["valid"]:
                    if verbose:
                        msg = "Line {}: ISBN normalization failure ({}): {}"
                        msg = msg.format(reader.line_num, result["input_value"],
                                         ISBNHandling.ISBN_ERRORS[result["error_type"]])
                        print_r(msg)
                    continue
                else:
                    isbn = result["normalised"]
                if isbn not in self.isbn_map:
                    self.isbn_map[isbn] = line
                else:
                    if isbn not in duplicate_isbns:
                        duplicate_isbns.append(isbn)
                        if verbose:
                            print_y("ISBN duplicate found in DOAB: " + isbn)
        for duplicate in duplicate_isbns:
            # drop duplicates alltogether
            del(self.isbn_map[duplicate])

    def lookup(self, isbn):
        result = self.isbn_handling.test_and_normalize_isbn(isbn)
        if result["valid"]:
            norm_isbn = result["normalised"]
            if norm_isbn in self.isbn_map:
                lookup_result =  {
                    "book_title" : self.isbn_map[norm_isbn]["Title"],
                    "publisher": self.isbn_map[norm_isbn]["Publisher"],
                    "license_ref": self.isbn_map[norm_isbn]["License"]
                }
                return lookup_result
        return None

    def download_doab_csv(self, target):
        urlretrieve("http://www.doabooks.org/doab?func=csv", target)

class ISBNHandling(object):

    # regex for 13-digit, unsplit ISBNs
    ISBN_RE = re.compile(r"^97[89]\d{10}$")

    # regex for 13-digit ISBNs split with hyphens
    ISBN_SPLIT_RE = re.compile(r"^97[89]\-\d{1,5}\-\d{1,7}\-\d{1,6}\-\d{1}$")

    ISBN_ERRORS = {
        0: "Input is neither a valid split nor a valid unsplit 13-digit ISBN",
        1: "Too short (Must be 17 chars long including hyphens)",
        2: "Too long (Must be 17 chars long including hyphens)",
        3: "Input ISBN was split, but the segmentation is invalid"
    }

    def __init__(self, range_file_path, range_file_update=False):
        if not os.path.isfile(range_file_path) or range_file_update:
            self.download_range_file(range_file_path)
        with open(range_file_path, "r") as range_file:
            range_file_content = range_file.read()
            range_file_root = ET.fromstring(range_file_content)
            self.ean_elements = range_file_root.findall("./EAN.UCCPrefixes/EAN.UCC")
            self.registration_groups = range_file_root.findall("./RegistrationGroups/Group")

    def download_range_file(self, target):
        urlretrieve("http://www.isbn-international.org/export_rangemessage.xml", target)

    def test_and_normalize_isbn(self, isbn):
        """
        Take a string input and try to normalize it to a 13-digit, split ISBN.

        This method takes a string which is meant to represent a split or unsplit 13-digit ISBN. It
        applies a range of tests to verify its validity and then returns a normalized, split variant.

        The following tests will be applied:
            - Syntax (Regex)
            - Re-split and segmentation comparison (if input was split already)

        Args:
            isbn: A string potentially representing a 13-digit ISBN (split or unsplit).
        Returns:
            A dict with 3 keys:
                'valid': A boolean indicating if the input passed all tests.
                'input_value': The original input value
                'normalised': The normalised, split result. Will be present if 'valid' is True.
                'error_type': An int indicating why a test failed. Will be present if 'valid'
                              is False. Corresponds to a key in the ISBN_ERRORS dict.
        """
        ret = {"valid": False, "input_value": str(isbn)}
        stripped_isbn = isbn.strip()
        unsplit_isbn = stripped_isbn.replace("-", "")
        split_on_input = False
        if self.ISBN_SPLIT_RE.match(stripped_isbn):
            if len(stripped_isbn) < 17:
                ret["error_type"] = 1
                return ret
            elif len(stripped_isbn) > 17:
                ret["error_type"] = 2
                return ret
            else:
                split_on_input = True
        if self.ISBN_RE.match(unsplit_isbn):
            split_isbn = self.split_isbn(unsplit_isbn)["value"]
            if split_on_input and split_isbn != stripped_isbn:
                ret["error_type"] = 3
                return ret
            ret["normalised"] = split_isbn
            ret["valid"] = True
            return ret
        ret["error_type"] = 0
        return ret

    def isbn_has_valid_check_digit(self, isbn):
        """
        Take a string representing a 13-digit ISBN (without hyphens) and test if its check digit is
        correct.
        """
        if not self.ISBN_RE.match(isbn):
            raise ValueError(str(isbn) + " is no valid 13-digit ISBN!")
        checksum = 0
        for index, digit in enumerate(isbn):
            if index % 2 == 0:
                checksum += int(digit)
            else:
                checksum += 3 * int(digit)
        return checksum % 10 == 0

    def _get_range_length_from_rules(self, isbn_fragment, rules_element):
        value = int(isbn_fragment[:7])
        range_re = re.compile(r"(?P<min>\d{7})-(?P<max>\d{7})")
        for rule in rules_element.findall("Rule"):
            range_text = rule.find("Range").text
            range_match = range_re.match(range_text)
            if int(range_match["min"]) <= value <= int(range_match["max"]):
                length = rule.find("Length").text
                return int(length)
        # Shouldn't happen as the range file is meant to be comprehensive. Undefined ranges are marked
        # with a length of 0 instead.
        msg = ('Could not find a length definition for fragment "' + isbn_fragment + '" in the ISBN ' +
               'range file.')
        raise ValueError(msg)

    def split_isbn(self, isbn):
        """
        Take an unsplit, 13-digit ISBN and insert hyphens to correctly separate its parts.

        This method takes a 13-digit ISBN and returns a hyphenated variant (Example: 9782753518278 ->
        978-2-7535-1827-8). Since the segments of an ISBN may vary in length (except for the EAN prefix
        and the check digit), the official "RangeMessage" XML file provided by the ISBN organization is
        needed for reference.

        Args:
            isbn: A string representing a 13-digit ISBN.
        Returns:
            A dict with two keys: 'success' and 'result'. If the process was successful, 'success'
            will be True and 'result' will contain the hyphenated result string. Otherwise, 'success'
            will be False and 'result' will contain an error message stating the reason.
        """
        ret_value = {
            'success': False,
            'value': None
        }
        split_isbn = ""
        remaining_isbn = isbn

        if not self.ISBN_RE.match(isbn):
            ret_value['value'] = '"' + str(isbn) + '" is no valid 13-digit ISBN!'
            return ret_value
        for ean in self.ean_elements:
            prefix = ean.find("Prefix").text
            if remaining_isbn.startswith(prefix):
                split_isbn += prefix
                remaining_isbn = remaining_isbn[len(prefix):]
                rules = ean.find("Rules")
                length = self._get_range_length_from_rules(remaining_isbn, rules)
                if length == 0:
                    msg = ('Invalid ISBN: Remaining fragment "{}" for EAN prefix "{}" is inside a ' +
                           'range which is not marked for use yet')
                    ret_value['value'] = msg.format(remaining_isbn, prefix)
                    return ret_value
                group = remaining_isbn[:length]
                split_isbn += "-" + group
                remaining_isbn = remaining_isbn[length:]
                break
        else:
            msg = 'ISBN "{}" does not seem to have a valid prefix.'
            ret_value['value'] = msg.format(isbn)
            return ret_value
        for group in self.registration_groups:
            prefix = group.find("Prefix").text
            if split_isbn == prefix:
                rules = group.find("Rules")
                length = self._get_range_length_from_rules(remaining_isbn, rules)
                if length == 0:
                    msg = ('Invalid ISBN: Remaining fragment "{}" for registration group "{}" is ' +
                           'inside a range which is not marked for use yet')
                    ret_value['value'] = msg.format(remaining_isbn, split_isbn)
                    return ret_value
                registrant = remaining_isbn[:length]
                split_isbn += "-" + registrant
                remaining_isbn = remaining_isbn[length:]
                check_digit = remaining_isbn[-1:]
                publication_number = remaining_isbn[:-1]
                split_isbn += "-" + publication_number + "-" + check_digit
                ret_value['success'] = True
                ret_value['value'] = split_isbn
                return ret_value
        else:
            msg = 'ISBN "{}" does not seem to have a valid registration group element.'
            ret_value['value'] = msg.format(isbn)
            return ret_value

class CSVAnalysisResult(object):

    def __init__(self, blanks, dialect, has_header, enc, enc_conf):
        self.blanks = blanks
        self.dialect = dialect
        self.has_header = has_header
        self.enc = enc
        self.enc_conf = enc_conf

    def __str__(self):
        ret = "*****CSV file analysis*****\n"
        if self.dialect is not None:
            quote_consts = ["QUOTE_ALL", "QUOTE_MINIMAL", "QUOTE_NONE",
                            "QUOTE_NONNUMERIC"]
            quoting = self.dialect.quoting
            for const in quote_consts:
            # Seems hacky. Is there a more pythonic way to determine a
            # member const by its value?
                if hasattr(csv, const) and getattr(csv, const) == self.dialect.quoting:
                    quoting = const
            ret += ("CSV dialect sniffing:\ndelimiter => {dlm}\ndoublequote " +
                    "=> {dbq}\nescapechar => {esc}\nquotechar => {quc}\nquoting " +
                    "=> {quo}\nskip initial space => {sis}\n\n").format(
                        dlm=self.dialect.delimiter,
                        dbq=self.dialect.doublequote,
                        esc=self.dialect.escapechar,
                        quc=self.dialect.quotechar,
                        quo=quoting,
                        sis=self.dialect.skipinitialspace)

        if self.has_header:
            ret += "CSV file seems to have a header.\n\n"
        else:
            ret += "CSV file doesn't seem to have a header.\n\n"


        if self.blanks:
            ret += "Found " + str(self.blanks) + " empty lines in CSV file.\n\n"
        if self.enc:
            ret += ("Educated guessing of file character encoding: {} with " +
                    "a confidence of {}%\n").format(
                        self.enc,
                        int(self.enc_conf * 100))
        ret += "***************************"
        return ret

class ANSIColorFormatter(logging.Formatter):
    """
    A simple logging formatter using ANSI codes to colorize messages
    """

    def __init__(self):
        super().__init__(fmt="%(levelname)s: %(message)s", datefmt=None, style="%")

    FORMATS = {
        logging.ERROR: "\033[91m%(levelname)s: %(message)s\033[0m",
        logging.WARNING: "\033[93m%(levelname)s: %(message)s\033[0m",
        logging.INFO: "\033[94m%(levelname)s: %(message)s\033[0m",
        "DEFAULT": "%(levelname)s: %(message)s"
    }

    def format(self, record):
        self._style._fmt = self.FORMATS.get(record.levelno, self.FORMATS["DEFAULT"])
        return logging.Formatter.format(self, record)

class BufferedErrorHandler(MemoryHandler):
    """
    A modified MemoryHandler without automatic flushing.

    This handler serves the simple purpose of buffering error and critical
    log messages so that they can be shown to the user in collected form when
    the enrichment process has finished.
    """
    def __init__(self, target):
        MemoryHandler.__init__(self, 100000, target=target)
        self.setLevel(logging.ERROR)

    def shouldFlush(self, record):
        return False

class NoRedirection(HTTPErrorProcessor):
    """
    A dummy processor to suppress HTTP redirection.

    This handler serves the simple purpose of stopping redirection for
    easy extraction of shortDOI redirect targets.
    """
    def http_response(self, request, response):
        return response

    https_response = http_response

def get_normalised_DOI(doi_string):
    doi_string = doi_string.strip()
    doi_match = DOI_RE.match(doi_string)
    if doi_match:
        doi = doi_match.groupdict()["doi"]
        return doi.lower()
    shortdoi_match = SHORTDOI_RE.match(doi_string)
    if shortdoi_match:
        # Extract redirect URL to obtain original DOI
        shortdoi = shortdoi_match.groupdict()["shortdoi"]
        url = "https://doi.org/" + shortdoi
        opener = build_opener(NoRedirection)
        try:
            res = opener.open(url)
            if res.code == 301:
                doi_match = DOI_RE.match(res.headers["Location"])
                if doi_match:
                    doi = doi_match.groupdict()["doi"]
                    return doi.lower()
            return None
        except (HTTPError, URLError):
            return None
    return None

def is_wellformed_ISSN(issn_string):
    issn_match = ISSN_RE.match(issn_string)
    if issn_match is not None:
        return True
    return False

def is_valid_ISSN(issn_string):
    issn_match = ISSN_RE.match(issn_string)
    match_dict = issn_match.groupdict()
    check_digit = match_dict["check_digit"]
    if check_digit in ["X", "x"]:
        check_digit = 10
    else:
        check_digit = int(check_digit)
    digits = match_dict["first_part"] + match_dict["second_part"]
    factor = 8
    total = 0
    for digit in digits:
        total += int(digit) * factor
        factor -= 1
    mod = total % 11
    if mod == 0 and check_digit == 0:
        return True
    else:
        if 11 - mod == check_digit:
            return True
    return False

def analyze_csv_file(file_path, test_lines=1000, enc=None):
    try:
        csv_file = open(file_path, "rb")
    except IOError as ioe:
        error_msg = "Error: could not open file '{}': {}".format(file_path,
                                                                 ioe.strerror)
        return {"success": False, "error_msg": error_msg}

    guessed_enc = None
    guessed_enc_confidence = None
    blanks = 0
    if chardet:
        byte_content = b"" # in python3 chardet operates on bytes
        lines_processed = 0
        for line in csv_file:
            if line.strip(): # omit blank lines
                lines_processed += 1
                if lines_processed <= test_lines:
                    byte_content += line
            else:
                blanks += 1
        chardet_result = chardet.detect(byte_content)
        guessed_enc = chardet_result["encoding"]
        guessed_enc_confidence = chardet_result["confidence"]

    csv_file.close()

    if enc is not None:
        used_encoding = enc
    elif guessed_enc is not None:
        used_encoding = guessed_enc
    else:
        used_encoding = locale.getpreferredencoding()

    text_content = ""
    with open(file_path, "r", encoding=used_encoding) as csv_file:
        try:
            lines_processed = 0
            for line in csv_file:
                if line.strip(): # omit blank lines
                    lines_processed += 1
                    text_content += line
                    if lines_processed > test_lines:
                        break
        except UnicodeError as ue:
            error = ('A UnicodeError occured while trying to read the csv ' +
                     'file ("{}") - it seems the encoding we used ({}) is ' +
                     'not correct.')
            error_msg = error.format(str(ue), used_encoding)
            return {"success": False, "error_msg": error_msg}

    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(text_content)
        has_header = sniffer.has_header(text_content)
    except csv.Error as csve:
        error_msg = ("Error: An error occured while analyzing the file: '" +
                     str(csve) + "'. Maybe it is no valid CSV file?")
        return {"success": False, "error_msg": error_msg}
    result = CSVAnalysisResult(blanks, dialect, has_header, guessed_enc, guessed_enc_confidence)
    return {"success": True, "data": result}

def get_csv_file_content(file_name, enc=None, force_header=False, print_results=True):
    result = analyze_csv_file(file_name, enc=enc)
    if result["success"]:
        csv_analysis = result["data"]
        if print_results:
            print(csv_analysis)
    else:
        raise IOError(result["error_msg"])

    if enc is None:
        enc = csv_analysis.enc

    if enc is None:
        raise IOError("No encoding given for CSV file and automated detection failed.")

    dialect = csv_analysis.dialect

    csv_file = open(file_name, "r", encoding=enc)

    content = []
    reader = csv.reader(csv_file, dialect=dialect)
    header = []
    if csv_analysis.has_header or force_header:
        header.append(next(reader))
    for row in reader:
        content.append(row)
    csv_file.close()
    return (header, content)

def has_value(field):
    return len(field) > 0 and field != "NA"

def oai_harvest(basic_url, metadata_prefix=None, oai_set=None, processing=None):
    """
    Harvest OpenAPC records via OAI-PMH
    """
    collection_xpath = ".//oai_2_0:metadata//intact:collection"
    record_xpath = ".//oai_2_0:record"
    identifier_xpath = ".//oai_2_0:header//oai_2_0:identifier"
    token_xpath = ".//oai_2_0:resumptionToken"
    processing_regex = re.compile(r"'(?P<target>\w*?)':'(?P<generator>.*?)'")
    variable_regex = re.compile(r"%(\w*?)%")
    #institution_xpath =
    namespaces = {
        "oai_2_0": "http://www.openarchives.org/OAI/2.0/",
        "intact": "http://intact-project.org"
    }
    url = basic_url + "?verb=ListRecords"
    if metadata_prefix:
        url += "&metadataPrefix=" + metadata_prefix
    if oai_set:
        url += "&set=" + oai_set
    if processing:
        match = processing_regex.match(processing)
        if match:
            groupdict = match.groupdict()
            target = groupdict["target"]
            generator = groupdict["generator"]
            variables = variable_regex.search(generator).groups()
        else:
            print_r("Error: Unable to parse processing instruction!")
            processing = None
    print_b("Harvesting from " + url)
    articles = []
    while url is not None:
        try:
            request = Request(url)
            url = None
            response = urlopen(request)
            content_string = response.read()
            root = ET.fromstring(content_string)
            records = root.findall(record_xpath, namespaces)
            counter = 0
            for record in records:
                article = {}
                identifier = record.find(identifier_xpath, namespaces)
                article["identifier"] = identifier.text
                collection = record.find(collection_xpath, namespaces)
                if collection is None:
                    # Might happen with deleted records
                    continue
                for elem, xpath in OAI_COLLECTION_CONTENT.items():
                    article[elem] = "NA"
                    if xpath is not None:
                        result = collection.find(xpath, namespaces)
                        if result is not None and result.text is not None:
                            article[elem] = result.text
                if processing:
                    target_string = generator
                    for variable in variables:
                        target_string = target_string.replace("%" + variable + "%", article[variable])
                    article[target] = target_string
                if article["euro"] in ["NA", "0"]:
                    print_r("Article skipped, no APC amount found.")
                    continue
                if article["doi"] != "NA":
                    norm_doi = get_normalised_DOI(article["doi"])
                    if norm_doi is None:
                        article["doi"] = "NA"
                    else:
                        article["doi"] = norm_doi
                articles.append(article)
                counter += 1
            token = root.find(token_xpath, namespaces)
            if token is not None and token.text is not None:
                url = basic_url + "?verb=ListRecords&resumptionToken=" + token.text
            print_g(str(counter) + " articles harvested.")
        except HTTPError as httpe:
            code = str(httpe.getcode())
            print("HTTPError: {} - {}".format(code, httpe.reason))
        except URLError as urle:
            print("URLError: {}".format(urle.reason))
    return articles

def find_book_dois_in_crossref(isbn_list):
    """
    Take a list of ISBNs and try to obtain book/monograph DOIs from crossref.

    Args:
        isbn_list: A list of strings representing ISBNs (will not be tested for validity).
    Returns:
        A dict with a key 'success'. If the lookup was successful,
        'success' will be True and the dict will have a second entry 'dois'
        which contains a list of obtained DOIs as strings. The list may be empty if the lookup
        returned an empty result.
        If an error occured during lookup, 'success' will be False and the dict will
        contain a second entry 'error_msg' with a string value
        stating the reason.
    """
    if type(isbn_list) != type([]) or len(isbn_list) == 0:
        raise ValueError("Parameter must be a non-empty list!")
    filter_list = ["isbn:" + isbn.strip() for isbn in isbn_list]
    filters = ",".join(filter_list)
    api_url = "https://api.crossref.org/works?filter="
    url = api_url + filters + "&rows=500"
    request = Request(url)
    request.add_header("User-Agent", USER_AGENT)
    ret_value = {
        "success": False,
        "dois": []
    }
    try:
        ret = urlopen(request)
        content = ret.read()
        data = json.loads(content)
        if data["message"]["total-results"] == 0:
            ret_value["success"] = True
        else:
            for item in data["message"]["items"]:
                if item["type"] in ["monograph", "book"] and item["DOI"] not in ret_value["dois"]:
                    ret_value["dois"].append(item["DOI"])
            if len(ret_value["dois"]) == 0:
                msg = "No monograph/book DOI type found in  Crossref ISBN search result ({})!"
                raise ValueError(msg.format(url))
            else:
                ret_value["success"] = True
    except HTTPError as httpe:
        ret_value['error_msg'] = "HTTPError: {} - {}".format(httpe.code, httpe.reason)
    except URLError as urle:
        ret_value['error_msg'] = "URLError: {}".format(urle.reason)
    except ValueError as ve:
        ret_value['error_msg'] = str(ve)
    return ret_value

def get_metadata_from_crossref(doi_string):
    """
    Take a DOI and extract metadata relevant to OpenAPC from crossref.

    This method looks up a DOI in crossref and returns all metadata fields
    relevant to OpenAPC. The set of metadata returned depends on the crossref
    DOI type.

    Args:
        doi_string: A string representing a DOI. 'Pure' form (10.xxx),
        DOI Handbook notation (doi:10.xxx) or crossref-style
        (https://doi.org/10.xxx) are all acceptable.
    Returns:
        A dict with a key 'success'. If data extraction was successful,
        'success' will be True and the dict will have a second entry 'data'
        which contains the extracted metadata plus the doi type as another dict:

        {'doi_type': 'journal_article',
         'publisher': 'MDPI AG',
         'journal_full_title': 'Chemosensors',
         [...]
        }
        The dict will contain all keys in question, those where no data could
        be retreived will have a None value.

        If data extraction failed, 'success' will be False and the dict will
        contain a second entry 'error_msg' with a string value
        stating the reason.
    """
    xpaths_article = {
        ".//cr_qr:crm-item[@name='publisher-name']": "publisher",
        ".//cr_qr:crm-item[@name='prefix-name']": "prefix",
        ".//cr_1_0:journal_metadata//cr_1_0:full_title": "journal_full_title",
        ".//cr_1_1:journal_metadata//cr_1_1:full_title": "journal_full_title",
        ".//cr_1_0:journal_metadata//cr_1_0:issn": "issn",
        ".//cr_1_1:journal_metadata//cr_1_1:issn": "issn",
        ".//cr_1_0:journal_metadata//cr_1_0:issn[@media_type='print']": "issn_print",
        ".//cr_1_1:journal_metadata//cr_1_1:issn[@media_type='print']": "issn_print",
        ".//cr_1_0:journal_metadata//cr_1_0:issn[@media_type='electronic']": "issn_electronic",
        ".//cr_1_1:journal_metadata//cr_1_1:issn[@media_type='electronic']": "issn_electronic",
        ".//ai:license_ref": "license_ref"
    }
    xpaths_book = {
        ".//cr_qr:crm-item[@name='prefix-name']": "prefix",
        ".//cr_1_0:book//cr_1_0:book_metadata//cr_1_0:publisher//cr_1_0:publisher_name": "publisher",
        ".//cr_1_1:book//cr_1_1:book_metadata//cr_1_1:publisher//cr_1_1:publisher_name": "publisher",
        ".//cr_1_0:book//cr_1_0:book_series_metadata//cr_1_0:publisher//cr_1_0:publisher_name": "publisher",
        ".//cr_1_1:book//cr_1_1:book_series_metadata//cr_1_1:publisher//cr_1_1:publisher_name": "publisher",
        ".//cr_1_0:book//cr_1_0:book_set_metadata//cr_1_0:publisher//cr_1_0:publisher_name": "publisher",
        ".//cr_1_1:book//cr_1_1:book_set_metadata//cr_1_1:publisher//cr_1_1:publisher_name": "publisher",
        ".//cr_1_0:book//cr_1_0:book_metadata//cr_1_0:titles//cr_1_0:title": "book_title",
        ".//cr_1_1:book//cr_1_1:book_metadata//cr_1_1:titles//cr_1_1:title": "book_title",
        ".//cr_1_0:book//cr_1_0:book_series_metadata/cr_1_0:titles/cr_1_0:title": "book_title",
        ".//cr_1_1:book//cr_1_1:book_series_metadata/cr_1_1:titles/cr_1_1:title": "book_title",
        ".//cr_1_0:book//cr_1_0:book_set_metadata/cr_1_0:titles//cr_1_0:title": "book_title",
        ".//cr_1_1:book//cr_1_1:book_set_metadata/cr_1_1:titles//cr_1_1:title": "book_title",
        ".//cr_1_0:book//cr_1_0:book_metadata//cr_1_0:isbn": "isbn",
        ".//cr_1_1:book//cr_1_1:book_metadata//cr_1_1:isbn": "isbn",
        ".//cr_1_0:book//cr_1_0:book_series_metadata//cr_1_0:isbn": "isbn",
        ".//cr_1_1:book//cr_1_1:book_series_metadata//cr_1_1:isbn": "isbn",
        ".//cr_1_0:book//cr_1_0:book_set_metadata//cr_1_0:isbn": "isbn",
        ".//cr_1_1:book//cr_1_1:book_set_metadata//cr_1_1:isbn": "isbn",
        ".//cr_1_0:book//cr_1_0:book_metadata//cr_1_0:isbn[@media_type='print']": "isbn_print",
        ".//cr_1_1:book//cr_1_1:book_metadata//cr_1_1:isbn[@media_type='print']": "isbn_print",
        ".//cr_1_0:book//cr_1_0:book_series_metadata//cr_1_0:isbn[@media_type='print']": "isbn_print",
        ".//cr_1_1:book//cr_1_1:book_series_metadata//cr_1_1:isbn[@media_type='print']": "isbn_print",
        ".//cr_1_0:book//cr_1_0:book_metadata//cr_1_0:isbn[@media_type='electronic']": "isbn_electronic",
        ".//cr_1_1:book//cr_1_1:book_metadata//cr_1_1:isbn[@media_type='electronic']": "isbn_electronic",
        ".//cr_1_0:book//cr_1_0:book_series_metadata//cr_1_0:isbn[@media_type='electronic']": "isbn_electronic",
        ".//cr_1_1:book//cr_1_1:book_series_metadata//cr_1_1:isbn[@media_type='electronic']": "isbn_electronic",
        ".//ai:license_ref": "license_ref"
    }
    namespaces = {
        "cr_qr": "http://www.crossref.org/qrschema/3.0",
        "cr_1_1": "http://www.crossref.org/xschema/1.1",
        "cr_1_0": "http://www.crossref.org/xschema/1.0",
        "ai": "http://www.crossref.org/AccessIndicators.xsd"
    }
    doi_types = {
        "journal_article": xpaths_article,
        "book_title": xpaths_book
    }
    doi = get_normalised_DOI(doi_string)
    if doi is None:
        error_msg = "Parse Error: '{}' is no valid DOI".format(doi_string)
        return {"success": False, "error_msg": error_msg}
    url = 'http://data.crossref.org/' + doi
    req = Request(url)
    req.add_header("Accept", "application/vnd.crossref.unixsd+xml")
    ret_value = {'success': True}
    try:
        response = urlopen(req)
        content_string = response.read()
        root = ET.fromstring(content_string)
        doi_element = root.findall(".//cr_qr:doi", namespaces)
        doi_type = doi_element[0].attrib['type']
        if doi_type not in doi_types:
            msg = ('Unsupported DOI type "{}" (OpenAPC only supports the following types: {}')
            msg = msg.format(doi_type, ", ".join(doi_types.keys()))
            raise ValueError(msg)
        crossref_data = {"doi_type": doi_type}
        xpaths = doi_types[doi_type]
        for path, elem in xpaths.items():
            if elem not in crossref_data:
                crossref_data[elem] = None
            result = root.findall(path, namespaces)
            if result:
                crossref_data[elem] = result[0].text
                if elem == 'license_ref':
                    # If there's more than one license_ref element, prefer
                    # the one with the attribute applies_to="vor"
                    for xml_elem in result:
                        if xml_elem.get("applies_to") == "vor":
                            crossref_data[elem] = xml_elem.text
                            break
        ret_value['data'] = crossref_data
    except HTTPError as httpe:
        ret_value['success'] = False
        ret_value['error_msg'] = "HTTPError: {} - {}".format(httpe.code, httpe.reason)
    except URLError as urle:
        ret_value['success'] = False
        ret_value['error_msg'] = "URLError: {}".format(urle.reason)
    except ET.ParseError as etpe:
        ret_value['success'] = False
        ret_value['error_msg'] = "ElementTree ParseError: {}".format(str(etpe))
    except ValueError as ve:
        ret_value['success'] = False
        ret_value['error_msg'] = str(ve)
    return ret_value

def get_metadata_from_pubmed(doi_string):
    """
    Look up a DOI in Europe PMC and extract Pubmed ID and Pubmed Central ID

    Args:
        doi_string: A string representing a doi. 'Pure' form (10.xxx),
        DOI Handbook notation (doi:10.xxx) or crossref-style
        (https://doi.org/10.xxx) are all acceptable.
    Returns:
        A dict with a key 'success'. If data extraction was successful,
        'success' will be True and the dict will have a second entry 'data'
        which contains the extracted metadata (pmid, pmcid) as another dict.

        If data extraction failed, 'success' will be False and the dict will
        contain a second entry 'error_msg' with a string value
        stating the reason.
    """
    doi = get_normalised_DOI(doi_string)
    if doi is None:
        return {"success": False,
                "error_msg": "Parse Error: '{}' is no valid DOI".format(doi_string)
               }
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=doi:"
    url += doi
    req = Request(url)
    ret_value = {'success': True}
    try:
        response = urlopen(req)
        content_string = response.read()
        root = ET.fromstring(content_string)
        pubmed_data = {}
        xpaths = {
            "pmid": ".//resultList/result/pmid",
            "pmcid": ".//resultList/result/pmcid",
        }
        for elem, path in xpaths.items():
            result = root.findall(path)
            if result:
                pubmed_data[elem] = result[0].text
            else:
                pubmed_data[elem] = None
        ret_value['data'] = pubmed_data
    except HTTPError as httpe:
        ret_value['success'] = False
        ret_value['error_msg'] = "HTTPError: {} - {}".format(httpe.code, httpe.reason)
    except URLError as urle:
        ret_value['success'] = False
        ret_value['error_msg'] = "URLError: {}".format(urle.reason)
    return ret_value

def get_euro_exchange_rates(currency, frequency="D"):
    """
    Obtain historical euro exchange rates against a certain currency from the European Central Bank.
    
    Take a currency and a frequency type (either daily, monthly average or yearly average rates) and
    return a dict containing all data provided by the ECB for the chosen parameters.
    
    Args:
        currency: A three-letter string representing a currency code according to ISO 4217
        frequency: Must be either "D" (daily), "M" (monthly) or "A" (annual). In the last two cases
                   the results will be average values for the given time frames.
    
    Returns:
        A dict of date strings mapping to exchange rates (as floats). Depending on the chosen
        freqency, the date format will either be "YYYY", "YYYY-MM" or "YYYY-MM-DD".
    """
    ISO_4217_RE = re.compile(r"[A-Z]{3}")
    FREQUENCIES = ["D", "M", "A"]
    
    URL_TEMPLATE = "http://sdw-wsrest.ecb.europa.eu/service/data/EXR/{}.{}.EUR.SP00.A?format=csvdata"
    
    if not ISO_4217_RE.match(currency):
        raise ValueError('"' + currency + '" is no valid currency code!')
    if frequency not in FREQUENCIES:
        raise ValueError("Frequency must be one of " + ", ".join(FREQUENCIES))
    
    url = URL_TEMPLATE.format(frequency, currency)
    req = Request(url)
    response = urlopen(req)
    lines = []
    for line in response:
        lines.append(line.decode("utf-8"))
    reader = csv.DictReader(lines)
    result = {}
    for line in reader:
        date = line["TIME_PERIOD"]
        value = line["OBS_VALUE"]
        result[date] = value
    return result

def _process_euro_value(euro_value, round_monetary, row_num, index, offsetting_mode):
    if not has_value(euro_value):
        msg = "Line %s: Empty monetary value in column %s."
        if offsetting_mode is None:
            logging.error(msg, row_num, index)
        else:
            logging.warning(msg, row_num, index)
        return "NA"
    try:
        # Cast to float to ensure the decimal point is a dot (instead of a comma)
        euro = locale.atof(euro_value)
        if euro.is_integer():
            euro = int(euro)
        if re.match(r"^\d+\.\d{3}", str(euro)):
            if round_monetary:
                euro = round(euro, 2)
                msg = "Line %s: " + MESSAGES["digits_norm"]
                logging.warning(msg, row_num, euro_value, euro_value, euro)
            else:
                msg = "Line %s: " + MESSAGES["digits_error"]
                logging.error(msg, row_num, euro_value)
        if euro == 0:
            msg = "Line %s: Euro value is 0"
            if offsetting_mode is None:
                logging.error(msg, row_num)
            else:
                logging.warning(msg, row_num)
        return str(euro)
    except ValueError:
        msg = "Line %s: " + MESSAGES["locale"]
        logging.error(msg, row_num, euro_value, index)
        return "NA"

def _process_period_value(period_value, row_num):
    if re.match(r"^\d{4}-[0-1]{1}\d(-[0-3]{1}\d)?$", period_value):
        msg = "Line %s: " + MESSAGES["period_format"]
        new_value = period_value[:4]
        logging.info(msg, row_num, period_value, new_value)
        return new_value
    return period_value

def _process_hybrid_status(hybrid_status, row_num):
    if not has_value(hybrid_status):
        msg = "Line %s: " + MESSAGES["no_hybrid_identifier"]
        logging.error(msg, row_num)
        return "NA"
    norm_value = get_hybrid_status_from_whitelist(hybrid_status)
    if norm_value is None:
        msg = "Line %s: " + MESSAGES["unknown_hybrid_identifier"]
        logging.error(msg, row_num, hybrid_status)
        return hybrid_status
    if norm_value != hybrid_status:
        msg = "Line %s: " + MESSAGES["hybrid_normalisation"]
        logging.warning(msg, row_num, hybrid_status, norm_value)
        return norm_value
    return hybrid_status

def _process_crossref_results(current_row, row_num, prefix, key, value):
    new_value = "NA"
    if value is not None:
        if key == "journal_full_title":
            unified_value = get_unified_journal_title(value)
            if unified_value != value:
                msg = MESSAGES["unify"].format("journal title", value, unified_value)
                logging.warning(msg)
            new_value = unified_value
        elif key == "publisher":
            unified_value = get_unified_publisher_name(value)
            if unified_value != value:
                msg = MESSAGES["unify"].format("publisher name", value, unified_value)
                logging.warning(msg)
            new_value = unified_value
            # Treat Springer Nature special case: crossref erroneously
            # reports publisher "Springer Nature" even for articles
            # published before 2015 (publishers fusioned only then)
            if int(current_row["period"]) < 2015 and new_value == "Springer Nature":
                publisher = None
                if prefix in ["Springer (Biomed Central Ltd.)", "Springer-Verlag", "Springer - Psychonomic Society"]:
                    publisher = "Springer Science + Business Media"
                elif prefix in ["Nature Publishing Group", "Nature Publishing Group - Macmillan Publishers"]:
                    publisher = "Nature Publishing Group"
                if publisher:
                    msg = "Line %s: " + MESSAGES["springer_distinction"]
                    logging.warning(msg, row_num, publisher, prefix)
                    new_value = publisher
                else:
                    msg = "Line %s: " + MESSAGES["unknown_prefix"]
                    logging.error(msg, row_num, prefix)
        # Fix ISSNs without hyphen
        elif key in ["issn", "issn_print", "issn_electronic"]:
            new_value = value
            if re.match(r"^\d{7}[\dxX]$", value):
                new_value = value[:4] + "-" + value[4:]
                msg = "Line %s: " + MESSAGES["issn_hyphen_fix"]
                logging.warning(msg, row_num, key, value, new_value)
        else:
            new_value = value
    return new_value

def _isbn_lookup(current_row, row_num, additional_isbns, isbn_handling):
    collected_isbns = []
    for isbn_field in ["isbn", "isbn_print", "isbn_electronic"]:
        if has_value(current_row[isbn_field]):
            collected_isbns.append(current_row[isbn_field])
    for isbn in additional_isbns:
        if has_value(isbn):
            collected_isbns.append(isbn)
    if len(collected_isbns) == 0:
        msg = ("Line %s: Neither a DOI nor an ISBN found, assuming default record type " +
               "journal_article")
        logging.warning(msg, row_num)
        return (None, "journal_article")
    query_isbns = []
    for isbn in collected_isbns:
        res = isbn_handling.test_and_normalize_isbn(isbn)
        if not res["valid"]:
            msg = "Invalid ISBN {}: {}".format(isbn, ISBNHandling.ISBN_ERRORS[res["error_type"]])
            logging.warning(msg)
        else:
            query_isbns.append(res["input_value"])
            if res["input_value"] != res["normalised"]:
                query_isbns.append(res["normalised"])
    cr_res = find_book_dois_in_crossref(query_isbns)
    if not cr_res["success"]:
        msg = "Line %s: Error while trying to look up ISBNs in Crossref: %s"
        logging.error(msg, row_num, cr_res["error_msg"])
        return (None, "book_title")
    elif len(cr_res["dois"]) == 0:
        msg = "Line %s: Performed Crossref ISBN lookup, no DOI found."
        logging.info(msg, row_num)
        return (None, "book_title")
    elif len(cr_res["dois"]) > 1:
        msg = "Line %s: Performed Crossref ISBN lookup, more than one DOI found (%s) -> Used first in list."
        logging.warning(msg, row_num, str(cr_res["dois"]))
        return (cr_res["dois"][0], None)
    else:
        msg = "Line %s: Performed Crossref ISBN lookup, DOI found (%s)."
        logging.info(msg, row_num, cr_res["dois"][0])
        return (cr_res["dois"][0], None)

def _process_isbn(row_num, isbn, isbn_handling):
    if not has_value(isbn):
        return "NA"
    # handle a potential white-space split
    isbn = isbn.replace(" ", "")
    norm_res = isbn_handling.test_and_normalize_isbn(isbn)
    if norm_res["valid"]:
        if norm_res["normalised"] != norm_res["input_value"]:
            msg = "Line %s: Normalisation: ISBN value tested and split (%s -> %s)"
            logging.info(msg, row_num, norm_res["input_value"], norm_res["normalised"])
        return norm_res["normalised"]
    else:
        # in case of an invalid split: Use the correct one. In all other cases: Drop the value
        if norm_res["error_type"] == 4:
            unsplit_isbn = isbn.replace("-", "")
            new_res = isbn_handling.test_and_normalize_isbn(unsplit_isbn)
            msg = "Line %s: ISBN value had an invalid split, used the correct one (%s -> %s)"
            logging.info(msg, row_num, isbn, new_res["normalised"])
            return new_res["normalised"]
        else:
            msg = "Line %s: Invalid ISBN value (%s), set to NA (reason: %s)"
            logging.warning(msg, row_num, norm_res["input_value"],
                            ISBNHandling.ISBN_ERRORS[norm_res["error_type"]])
            return "NA"

def process_row(row, row_num, column_map, num_required_columns, additional_isbn_columns,
                doab_analysis, doaj_analysis, no_crossref_lookup=False, no_pubmed_lookup=False,
                no_doaj_lookup=False, round_monetary=False, offsetting_mode=None, crossref_max_retries=3):
    """
    Enrich a single row of data and reformat it according to OpenAPC standards.

    Take a csv row (a list) and a column mapping (a dict of CSVColumn objects)
    and return an enriched and re-arranged version which conforms to the Open
    APC data schema. The method will decide on which data schema to use depending
    on the identified publication type.

    Args:
        row: A list of column values (as yielded by a UnicodeReader f.e.).
        row_num: The line number in the csv file, for logging purposes.
        column_map: A dict of CSVColumn Objects, mapping the row
                    cells to OpenAPC data schema fields.
        num_required_columns: An int describing the required length of the row
                              list. If not matched, an error is logged and the
                              row is returned unchanged.
        additional_isbn_columns: A list of ints designating row indexes as additional ISBN sources.
        doab_analysis: A DOABanalysis object to perform an offline DOAB lookup
        doaj_analysis: A DOAJAnalysis object to perform offline DOAJ lookups
        no_crossref_lookup: If true, no metadata will be imported from crossref.
        no_pubmed_lookup: If true, no_metadata will be imported from pubmed.
        no_doaj_lookup: If true, journals will not be checked for being
                        listended in the DOAJ.
        round_monetary: If true, monetary values with more than 2 digits behind the decimal
                        mark will be rounded. If false, these cases will be treated as errors.
        offsetting_mode: If not None, the row is assumed to originate from an offsetting file
                         and this argument's value will be added to the 'agreement' column
        crossref_max_retries: Max number of attempts to query the crossref API if a 504 error
                              is received.
     Returns:
        A list of values which represents the enriched and re-arranged variant
        of the input row. If no errors were logged during the process, this
        result will conform to the OpenAPC data schema.
    """
    if len(row) != num_required_columns:
        msg = "Line %s: " + MESSAGES["num_columns"]
        logging.error(msg, row_num, len(row), num_required_columns)
        return row

    current_row = {}
    record_type = None

    # Copy content of identified columns and apply special processing rules
    for csv_column in column_map.values():
        index, column_type = csv_column.index, csv_column.column_type
        if column_type == "euro" and index is not None:
            current_row["euro"] = _process_euro_value(row[index], round_monetary, row_num, index, offsetting_mode)
        elif column_type == "period":
            current_row["period"] = _process_period_value(row[index], row_num)
        elif column_type == "is_hybrid" and index is not None:
            current_row["is_hybrid"] = _process_hybrid_status(row[index], row_num)
        else:
            if index is not None and len(row[index]) > 0:
                current_row[column_type] = row[index]
            else:
                current_row[column_type] = "NA"

    doi = current_row["doi"]
    if len(doi) == 0 or doi == 'NA':
        # lookup ISBNs in crossref
        msg = ("Line %s: No DOI found")
        logging.info(msg, row_num)
        current_row["indexed_in_crossref"] = "FALSE"
        additional_isbns = [row[i] for i in additional_isbn_columns]
        found_doi, r_type = _isbn_lookup(current_row, row_num, additional_isbns, doab_analysis.isbn_handling)
        if r_type is not None:
            record_type = r_type
        if found_doi is not None:
            # integrate DOI into row and restart
            logging.info("New DOI integrated, restarting enrichment for current line.")
            index = column_map["doi"].index
            row[index] = found_doi
            return process_row(row, row_num, column_map, num_required_columns, additional_isbn_columns,
                doab_analysis, doaj_analysis, no_crossref_lookup, no_pubmed_lookup,
                no_doaj_lookup, round_monetary, offsetting_mode)
    if has_value(doi):
        # Normalise DOI
        norm_doi = get_normalised_DOI(doi)
        if norm_doi is not None and norm_doi != doi:
            current_row["doi"] = norm_doi
            msg = MESSAGES["doi_norm"].format(doi, norm_doi)
            logging.info(msg)
            doi = norm_doi
        # include crossref metadata
        if not no_crossref_lookup:
            crossref_result = get_metadata_from_crossref(doi)
            retries = 0
            while not crossref_result["success"] and crossref_result["error_msg"].startswith("HTTPError: 504"):
                if retries >= crossref_max_retries:
                    break
                # retry on gateway timeouts, crossref API is quite busy sometimes
                msg = "%s, retrying..."
                logging.warning(msg, crossref_result["error_msg"])
                retries += 1
                crossref_result = get_metadata_from_crossref(doi)
            if crossref_result["success"]:
                data = crossref_result["data"]
                record_type = data.pop("doi_type")
                logging.info("Crossref: DOI resolved: " + doi + " [" + record_type + "]")
                current_row["indexed_in_crossref"] = "TRUE"
                prefix = data.pop("prefix")
                for key, value in data.items():
                    new_value = _process_crossref_results(current_row, row_num, prefix, key, value)
                    old_value = current_row[key]
                    current_row[key] = column_map[key].check_overwrite(old_value, new_value)
            else:
                msg = "Line %s: Crossref: Error while trying to resolve DOI %s: %s"
                logging.error(msg, row_num, doi, crossref_result["error_msg"])
                current_row["indexed_in_crossref"] = "FALSE"
                # lookup ISBNs in crossref and try to find a correct DOI
                additional_isbns = [row[i] for i in additional_isbn_columns]
                found_doi, r_type = _isbn_lookup(current_row, row_num, additional_isbns, doab_analysis.isbn_handling)
                if r_type is not None:
                    record_type = r_type
                if found_doi is not None:
                    # integrate DOI into row and restart
                    logging.info("New DOI integrated, restarting enrichment for current line.")
                    index = column_map["doi"].index
                    row[index] = found_doi
                    return process_row(row, row_num, column_map, num_required_columns, additional_isbn_columns,
                                       doab_analysis, doaj_analysis, no_crossref_lookup, no_pubmed_lookup,
                                       no_doaj_lookup, round_monetary, offsetting_mode)
        # include pubmed metadata
        if not no_pubmed_lookup:
            pubmed_result = get_metadata_from_pubmed(doi)
            if pubmed_result["success"]:
                logging.info("Pubmed: DOI resolved: " + doi)
                data = pubmed_result["data"]
                for key, value in data.items():
                    if value is not None:
                        new_value = value
                    else:
                        new_value = "NA"
                        msg = "WARNING: Element %s not found in in response for doi %s."
                        logging.debug(msg, key, doi)
                    old_value = current_row[key]
                    current_row[key] = column_map[key].check_overwrite(old_value, new_value)
            else:
                msg = "Line %s: Pubmed: Error while trying to resolve DOI %s: %s"
                logging.error(msg, row_num, doi, pubmed_result["error_msg"])

    # lookup in DOAJ. try the EISSN first, then ISSN and finally print ISSN
    if not no_doaj_lookup:
        issns = []
        new_value = "NA"
        if current_row["issn_electronic"] != "NA":
            issns.append(current_row["issn_electronic"])
        if current_row["issn"] != "NA":
            issns.append(current_row["issn"])
        if current_row["issn_print"] != "NA":
            issns.append(current_row["issn_print"])
        for issn in issns:
            lookup_result = doaj_analysis.lookup(issn)
            if lookup_result:
                msg = "DOAJ: Journal ISSN (%s) found in DOAJ offline copy ('%s')."
                logging.info(msg, issn, lookup_result)
                new_value = "TRUE"
                break
            else:
                msg = "DOAJ: Journal ISSN (%s) not found in DOAJ offline copy."
                new_value = "FALSE"
                logging.info(msg, issn)
        old_value = current_row["doaj"]
        current_row["doaj"] = column_map["doaj"].check_overwrite(old_value, new_value)
    if record_type != "journal_article":
        collected_isbns = []
        for isbn_field in ["isbn", "isbn_print", "isbn_electronic"]:
            # test and split all ISBNs
            current_row[isbn_field] = _process_isbn(row_num, current_row[isbn_field], doab_analysis.isbn_handling)
            if has_value(current_row[isbn_field]):
                collected_isbns.append(current_row[isbn_field])
        additional_isbns = [row[i] for i in additional_isbn_columns]
        for isbn in additional_isbns:
            result = _process_isbn(row_num, isbn, doab_analysis.isbn_handling)
            if has_value(result):
                collected_isbns.append(result)
        if len(collected_isbns) == 0:
            logging.info("No ISBN found, skipping DOAB lookup.")
            current_row["doab"] = "NA"
        else:
            record_type = "book_title"
            logging.info("Trying a DOAB lookup with the following values: " + str(collected_isbns))
            for isbn in collected_isbns:
                doab_result = doab_analysis.lookup(isbn)
                if doab_result is not None:
                    current_row["doab"] = "TRUE"
                    msg = 'DOAB: ISBN %s found in normalized DOAB (%s, "%s")'
                    logging.info(msg, isbn, doab_result["publisher"], doab_result["book_title"])
                    if current_row["indexed_in_crossref"] == "TRUE":
                        msg = "Book already found in Crossref via DOI, those results take precedence"
                        logging.info(msg)
                    else:
                        for key in doab_result:
                            current_row[key] = doab_result[key]
                    if not has_value(current_row["isbn"]):
                        current_row["isbn"] = isbn
                    break
            else:
                current_row["doab"] = "FALSE"
                msg = "DOAB: None of the ISBNs found in DOAB"
                logging.info(msg)
    if offsetting_mode:
        current_row["agreement"] = offsetting_mode
        record_type = "journal_article_transagree"

    if record_type is None:
        msg = "Line %s: Could not identify record type, using default schema 'journal_article'"
        logging.error(msg, row_num)
        record_type = "journal_article"

    result = []
    for field in COLUMN_SCHEMAS[record_type]:
        result.append(current_row[field])

    return (record_type, result)

def get_hybrid_status_from_whitelist(hybrid_status):
    """
    Obtain a boolean identifier for journal hybrid status by looking up possible
    synonyms in a whitelist.
    Args:
        hybrid status: A string describing the hybrid status of a journal.
    Returns:
        An OpenAPC-normalised boolean identifer (TRUE/FALSE) if the designation was found
        in a whitelist.
    """
    for boolean_value, whitelist in mappings.HYBRID_STATUS.items():
        if hybrid_status.strip().lower() in whitelist:
            return boolean_value
    return None

def get_column_type_from_whitelist(column_name):
    """
    Identify a CSV column type by looking up the name in a whitelist.

    Args:
        column_name: Name of a CSV column, usually extracted from the header.
    Returns:
        An APC-normed column type (as a string) if the column name was found in
        a whitelist, None otherwise.
    """
    for key, whitelist in mappings.COLUMN_NAMES.items():
        if column_name.strip().lower() in whitelist:
            return key
    return None

def get_unified_publisher_name(publisher):
    """
    Unify certain publisher names via a mapping table.

    CrossRef data is sometimes inconsistent when it comes to publisher names,
    these cases can be solved by returning a unified name from a mapping table.

    Args:
        publisher: A publisher as it is returned from the CrossRef API.
    Returns:
        Either a unified name or the original name as a string
    """
    return mappings.PUBLISHER_MAPPINGS.get(publisher, publisher)

def get_unified_journal_title(journal_full_title):
    """
    Unify certain journal titles via a mapping table.

    CrossRef data is sometimes inconsistent when it comes to journal titles,
    these cases can be solved by returning a unified name from a mapping table.

    Args:
        journal_full_title: A journal title as it is returned from the CrossRef API.
    Returns:
        Either a unified name or the original name as a string
    """

    return mappings.JOURNAL_MAPPINGS.get(journal_full_title, journal_full_title)

def get_corrected_issn_l(issn_l):
    return mappings.ISSN_L_CORRECTIONS.get(issn_l, issn_l)
    
def colorize(text, color):
    ANSI_COLORS = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "cyan": "\033[96m"
    }
    return ANSI_COLORS[color] + text + "\033[0m"

def print_b(text):
    print(colorize(text, "blue"))

def print_g(text):
    print(colorize(text, "green"))

def print_r(text):
    print(colorize(text, "red"))

def print_y(text):
    print(colorize(text, "yellow"))

def print_c(text):
    print(colorize(text, "cyan"))
