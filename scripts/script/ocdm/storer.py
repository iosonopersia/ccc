#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2016, Silvio Peroni <essepuntato@gmail.com>
#
# Permission to use, copy, modify, and/or distribute this software for any purpose
# with or without fee is hereby granted, provided that the above copyright notice
# and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
# REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
# FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT,
# OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE,
# DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS
# ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS
# SOFTWARE.

__author__ = 'essepuntato'

from SPARQLWrapper import SPARQLWrapper
from script.support.reporter import Reporter
import re
import os
from rdflib import BNode, ConjunctiveGraph
import shutil
import json
from datetime import datetime
import argparse
import io
from script.support.support import find_paths
from script.ocdm.conf import context_path, context_file_path, triplestore_url, temp_dir_for_rdf_loading
from rdflib.term import _toPythonMapping
from rdflib import XSD


class Storer(object):

    def __init__(self, graph_set=None, repok=None, reperr=None,
                 context_map={}, default_dir="_", dir_split=0, n_file_item=1, nt=False, nq=False):
        self.nt = nt
        self.nq = nq
        self.dir_split = dir_split
        self.n_file_item = n_file_item
        self.default_dir = default_dir

        if not nt and not nq:
            self.context_map = context_map
            for context_url in context_map:
                context_file_path = context_map[context_url]
                with open(context_file_path) as f:
                    context_json = json.load(f)
                    self.context_map[context_url] = context_json

        if graph_set is None:
            self.g = []
        else:
            self.g = graph_set.graphs()
        if repok is None:
            self.repok = Reporter(prefix="[Storer: INFO] ")
        else:
            self.repok = repok
        if reperr is None:
            self.reperr = Reporter(prefix="[Storer: ERROR] ")
        else:
            self.reperr = reperr
        self.preface_query = ""

    @staticmethod
    def hack_dates():
        if XSD.gYear in _toPythonMapping:
            _toPythonMapping.pop(XSD.gYear)
        if XSD.gYearMonth in _toPythonMapping:
            _toPythonMapping.pop(XSD.gYearMonth)

    def store_graphs_in_file(self, file_path, context_path):
        self.repok.new_article()
        self.reperr.new_article()
        self.repok.add_sentence("Store the graphs into a file: starting process")

        cg = ConjunctiveGraph()
        for g in self.g:
            cg.addN([item + (g.identifier,) for item in list(g)])

        self.__store_in_file(cg, file_path, context_path)

    def store_all(self, base_dir, base_iri, context_path, tmp_dir=None, g_set=[], override=False, remove_data=False):
        for g in g_set:
            self.g += [g]

        self.repok.new_article()
        self.reperr.new_article()

        self.repok.add_sentence("Starting the process")

        processed_graphs = {}
        for cur_g in self.g:
            processed_graphs = self.store(cur_g, base_dir, base_iri, context_path, tmp_dir,
                                          override, processed_graphs, False, remove_data)

        stored_graph_path = []
        for cur_file_path in processed_graphs:
            stored_graph_path += [cur_file_path]

            self.__store_in_file(processed_graphs[cur_file_path], cur_file_path, context_path)

        return stored_graph_path

    def upload_and_store(self, base_dir, triplestore_url, base_iri, context_path,
                         tmp_dir=None, g_set=[], override=False):

        stored_graph_path = self.store_all(base_dir, base_iri, context_path, tmp_dir, g_set, override)

        # Some graphs were not stored properly, then no one will be updloaded to the triplestore
        # but we highlights those ones that could be added in principle, by mentioning them
        # with a ".notupdloaded" marker
        if None in stored_graph_path:
            for file_path in stored_graph_path:
                # Create a marker for the file not uploaded in the triplestore
                open("%s.notuploaded" % file_path, "w").close()
                self.reperr.add_sentence("[6] "
                                         "The statements of in the JSON-LD file '%s' were not "
                                         "uploaded into the triplestore." % file_path)
        else:  # All the files have been stored
            self.upload_all(self.g, triplestore_url, base_dir)

    def query(self, query_string, triplestore_url, n_statements=None, base_dir=None):
        if query_string != "":
            try:
                tp = SPARQLWrapper(triplestore_url)
                tp.setMethod('POST')
                tp.setQuery(query_string)
                tp.query()

                if n_statements is None:
                    self.repok.add_sentence(
                        "Triplestore updated by means of a SPARQL Update query.")
                else:
                    self.repok.add_sentence(
                        "Triplestore updated with %s more RDF statements." % n_statements)

                return True

            except Exception as e:
                self.reperr.add_sentence("[1] "
                                         "Graph was not loaded into the "
                                         "triplestore due to communication problems: %s" % str(e))
                if base_dir is not None:
                    tp_err_dir = base_dir + os.sep + "tp_err"
                    if not os.path.exists(tp_err_dir):
                        os.makedirs(tp_err_dir)
                    cur_file_err = tp_err_dir + os.sep + \
                                   datetime.now().strftime('%Y-%m-%d-%H-%M-%S-%f_not_uploaded.txt')
                    with io.open(cur_file_err, "w", encoding="utf-8") as f:
                        f.write(query_string)

        return False

    def do_action_all(self, all_g, triplestore_url, base_dir, query_f):
        result = True

        self.repok.new_article()
        self.reperr.new_article()

        query_string = None
        total_new_statements = None

        for idx, cur_g in enumerate(all_g):
            cur_idx = idx % 10
            if cur_idx == 0:
                if query_string is not None:
                    result &= self.query(query_string, triplestore_url, total_new_statements, base_dir)
                query_string = ""
                total_new_statements = 0
            else:
                query_string += " ; "
                total_new_statements += len(cur_g)

            query_string += self.get_preface_query(cur_g) + query_f(cur_g)

        if query_string is not None and query_string != "":
            result &= self.query(query_string, triplestore_url, total_new_statements, base_dir)

        return result

    def update_all(self, all_add_g, all_remove_g, triplestore_url, base_dir):
        return self.do_action_all(all_remove_g, triplestore_url, base_dir, Storer._make_delete_query) and \
               self.upload_all(all_add_g, triplestore_url, base_dir)

    def upload_all(self, all_g, triplestore_url, base_dir):
        return self.do_action_all(all_g, triplestore_url, base_dir, Storer._make_insert_query)

    def execute_upload_query(self, query_string, triplestore_url):
        self.repok.new_article()
        self.reperr.new_article()

        return self.query(query_string, triplestore_url)

    def upload(self, cur_g, triplestore_url):
        self.repok.new_article()
        self.reperr.new_article()

        query_string = Storer._make_insert_query(cur_g)

        return self.query(query_string, triplestore_url, len(cur_g))

    def set_preface_query(self, query_string):
        self.preface_query = query_string

    def get_preface_query(self, cur_g):
        if self.preface_query != "":
            if type(cur_g.identifier) is BNode:
                return "CLEAR DEFAULT ; "
            else:
                return "WITH <%s> " % str(cur_g.identifier) + self.preface_query + " ; "
        else:
            return ""

    @staticmethod
    def _make_insert_query(cur_g):
        return Storer.__make_query(cur_g, "INSERT")

    @staticmethod
    def _make_delete_query(cur_g):
        return Storer.__make_query(cur_g, "DELETE")

    @staticmethod
    def __make_query(cur_g, query_type="INSERT"):
        if type(cur_g.identifier) is BNode:
            return "%s DATA { %s }" % (query_type, cur_g.serialize(format="nt11", encoding="utf-8").decode("utf-8"))
        else:
            return "%s DATA { GRAPH <%s> { %s } }" % \
                   (query_type, str(cur_g.identifier), cur_g.serialize(format="nt11", encoding="utf-8").decode("utf-8"))

    def __store_in_file(self, cur_g, cur_file_path, context_path):
        # Note: the following lines from here and until 'cur_json_ld' are a sort of hack for including all
        # the triples of the input graph into the final stored file. Some how, some of them are not written
        # in such file otherwise - in particular the provenance ones.
        new_g = ConjunctiveGraph()
        for s, p, o in cur_g.triples((None, None, None)):
            g_iri = None
            for g_context in cur_g.contexts((s, p, o)):
                g_iri = g_context.identifier
                break

            new_g.addN([(s, p, o, g_iri)])

        if not self.nt and not self.nq and context_path:
            cur_json_ld = json.loads(
                new_g.serialize(format="json-ld", context=self.__get_context(context_path)).decode("utf-8"))

            if isinstance(cur_json_ld, dict):
                cur_json_ld["@context"] = context_path
            else:  # it is a list
                for item in cur_json_ld:
                    item["@context"] = context_path

            with open(cur_file_path, "w") as f:
                json.dump(cur_json_ld, f, indent=4, ensure_ascii=False)
        elif self.nt:
            new_g.serialize(cur_file_path, format="nt11", encoding="utf-8")
        elif self.nq:
            new_g.serialize(cur_file_path, format="nquads", encoding="utf-8")

        self.repok.add_sentence("File '%s' added." % cur_file_path)

    def dir_and_file_paths(self, cur_g, base_dir, base_iri):
        cur_subject = set(cur_g.subjects(None, None)).pop()
        if self.nt or self.nq:
            is_json = False
        else:
            is_json = True
        return find_paths(
            str(cur_subject), base_dir, base_iri, self.default_dir, self.dir_split, self.n_file_item, is_json=is_json)

    def update(self, add_g, remove_g, base_dir, base_iri, context_path, tmp_dir=None,
               override=False, already_processed={}, store_now=True):
        self.repok.new_article()
        self.reperr.new_article()

        if len(remove_g) > 0:
            cur_dir_path, cur_file_path = self.dir_and_file_paths(remove_g, base_dir, base_iri)

            if cur_file_path in already_processed:
                final_g = already_processed[cur_file_path]
            elif os.path.exists(cur_file_path):
                # This is a conjunctive graps that contains all the triples (and graphs)
                # the file is actually defining - they could be more than those using
                # 'cur_subject' as subject.
                final_g = self.load(cur_file_path, tmp_dir=tmp_dir)
                already_processed[cur_file_path] = final_g

            for s, p, o, g in [item + (remove_g.identifier,) for item in list(remove_g)]:
                final_g.remove((s, p, o, g))

        if len(add_g) > 0:
            self.store(add_g, base_dir, base_iri, context_path, tmp_dir, override, already_processed, store_now)
        elif len(remove_g) > 0 and store_now:
            self.__store_in_file(final_g, cur_file_path, context_path)

        return already_processed

    def store(self, cur_g, base_dir, base_iri, context_path, tmp_dir=None,
              override=False, already_processed={}, store_now=True, remove_data=False):
        self.repok.new_article()
        self.reperr.new_article()

        if len(cur_g) > 0:
            cur_dir_path, cur_file_path = self.dir_and_file_paths(cur_g, base_dir, base_iri)

            try:
                if not os.path.exists(cur_dir_path):
                    os.makedirs(cur_dir_path)

                final_g = ConjunctiveGraph()
                final_g.addN([item + (cur_g.identifier,) for item in list(cur_g)])

                # Remove the data
                if remove_data:
                    stored_g = None
                    if cur_file_path in already_processed:
                        stored_g = already_processed[cur_file_path]
                    elif os.path.exists(cur_file_path):
                        stored_g = self.load(cur_file_path, cur_g, tmp_dir)

                    for s, p, o, g in final_g.quads((None, None, None, None)):
                        stored_g.remove((s, p, o, g))

                    final_g = stored_g
                elif not override: # Merging the data
                    if cur_file_path in already_processed:
                        stored_g = already_processed[cur_file_path]
                        stored_g.addN(final_g.quads((None, None, None, None)))
                        final_g = stored_g
                    elif os.path.exists(cur_file_path):
                        # This is a conjunctive graps that contains all the triples (and graphs)
                        # the file is actually defining - they could be more than those using
                        # 'cur_subject' as subject.
                        final_g = self.load(cur_file_path, cur_g, tmp_dir)

                already_processed[cur_file_path] = final_g

                if store_now:
                    self.__store_in_file(final_g, cur_file_path, context_path)

                return already_processed
            except Exception as e:
                self.reperr.add_sentence("[5] It was impossible to store the RDF statements in %s. %s" %
                                         (cur_file_path, str(e)))

        return None

    def __get_context(self, context_url):
        if context_url in self.context_map:
            return self.context_map[context_url]
        else:
            return context_url

    def __get_first_context(self):
        for context_url in self.context_map:
            return self.context_map[context_url]

    def load(self, rdf_file_path, cur_graph=None, tmp_dir=None):
        self.repok.new_article()
        self.reperr.new_article()

        if os.path.isfile(rdf_file_path):
            Storer.hack_dates()
            # The line above has been added for handling gYear and gYearMonth correctly.
            # More info at https://github.com/RDFLib/rdflib/issues/806.

            try:
                cur_graph = self.__load_graph(rdf_file_path, cur_graph)
            except IOError:
                if tmp_dir is not None:
                    current_file_path = tmp_dir + os.sep + "tmp_rdf_file.rdf"
                    shutil.copyfile(rdf_file_path, current_file_path)
                    try:
                        cur_graph = self.__load_graph(current_file_path, cur_graph)
                    except IOError as e:
                        self.reperr.add_sentence("[2] "
                                                 "It was impossible to handle the format used for "
                                                 "storing the file (stored in the temporary path) '%s'. "
                                                 "Additional details: %s"
                                                 % (current_file_path, str(e)))
                    os.remove(current_file_path)
                else:
                    self.reperr.add_sentence("[3] "
                                             "It was impossible to try to load the file from the "
                                             "temporary path '%s' since that has not been specified in "
                                             "advance" % rdf_file_path)
        else:
            self.reperr.add_sentence("[4] "
                                     "The file specified ('%s') doesn't exist."
                                     % rdf_file_path)

        return cur_graph

    def __load_graph(self, file_path, cur_graph=None):
        formats = ["json-ld", "rdfxml", "turtle", "trig", "nt11", "nquads"]

        current_graph = ConjunctiveGraph()

        if cur_graph is not None:
            current_graph.parse(data=cur_graph.serialize(format="trig"), format="trig")

        for cur_format in formats:
            try:
                if cur_format == "json-ld":
                    with open(file_path) as f:
                        json_ld_file = json.load(f)
                        if isinstance(json_ld_file, dict):
                            json_ld_file = [json_ld_file]

                        for json_ld_resource in json_ld_file:
                            # Trick to force the use of a pre-loaded context if the format
                            # specified is JSON-LD
                            context_json = None
                            if "@context" in json_ld_resource:
                                cur_context = json_ld_resource["@context"]
                                if cur_context in self.context_map:
                                    context_json = self.__get_context(cur_context)["@context"]
                                    json_ld_resource["@context"] = context_json

                            current_graph.parse(data=json.dumps(json_ld_resource, ensure_ascii=False),
                                                format=cur_format)
                else:
                    current_graph.parse(file_path, format=cur_format)

                return current_graph
            except Exception as e:
                errors = " | " + str(e)  # Try another format

        raise IOError("1", "It was impossible to handle the format used for storing the file '%s'%s" %
                      (file_path, errors))


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser("storer.py")
    arg_parser.add_argument("-i", "--input", dest="input", required=True,
                            help="The file containing the RDF to execute, the JSON-LD to upload, "
                                 "or a directory containing several files with both queries and RDF.")

    args = arg_parser.parse_args()

    if args.conf is not None:
        my_conf = __import__(args.conf)
        for attr in dir(my_conf):
            if not attr.startswith("__"):
                globals()[attr] = getattr(my_conf, attr)

    storer = Storer(repok=Reporter(True), reperr=Reporter(True),
                    context_map={context_path: context_file_path})

    all_files = []
    if os.path.isdir(args.input):
        for cur_dir, cur_subdir, cur_files in os.walk(args.input):
            for cur_file in cur_files:
                full_path = cur_dir + os.sep + cur_file
                if re.search(os.sep + "prov" + os.sep, full_path) is None and \
                        not full_path.endswith("index.json"):
                    all_files += [full_path]
    else:
        all_files += [args.input]

    for cur_file in all_files:
        if not os.path.basename(cur_file).startswith("index"):
            storer.repok.new_article()
            storer.repok.add_sentence("Processing file '%s'" % cur_file)
            if cur_file.endswith(".txt"):
                with io.open(cur_file, "r", encoding="utf-8") as f:
                    query_string = f.read()
                    storer.execute_upload_query(query_string, triplestore_url)
            elif cur_file.endswith(".json"):
                conj_g = storer.load(cur_file, tmp_dir=temp_dir_for_rdf_loading)
                for cur_g in conj_g.contexts():
                    storer.upload(cur_g, triplestore_url)

    storer.repok.write_file("storer_ok.txt")
    storer.reperr.write_file("storer_err.txt")
