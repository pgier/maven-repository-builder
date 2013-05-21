#!/usr/bin/env python

import urllib
import urlparse
from configuration import Configuration
from download import fetchArtifacts
from artifact_list_builder import ArtifactListBuilder
from filter import Filter


def createRepo():

    # load configuration
    config = Configuration()
    config.load()

    # build list
    listBuilder = ArtifactListBuilder(config)
    artifactList = listBuilder.buildList()

    #filter list
    listFilter = Filter(config)
    artifactList = listFilter.filter(artifactList)

    # fetch artifacts
    fetchArtifacts(artifactList, config)

    # package repository
    # TODO

    # test repository
    # TODO


def main():

    createRepo()


if __name__ == '__main__':
    main()
