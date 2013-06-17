import os
import re
import maven_repo_util as mrbutils
import logging
from subprocess import Popen
from subprocess import PIPE
from subprocess import call
from maven_artifact import MavenArtifact
from download import fetchArtifact

try:
    import koji
except ImportError:
    print "Unable to import koji lib, related features will be unavailable"


class ArtifactListBuilder:
    """
    Class loading artifact "list" from sources defined in the given
    configuration. The result is dictionary with following structure:

    "<groupId>:<artifactId>" (string)
      L <artifact source priority> (int)
         L <version> (string)
            L <repo url> (string)
    """

    _fileExtRegExp = "((?:tar\.)?[^.]+)$"

    def __init__(self, configuration):
        self.configuration = configuration

    def buildList(self):
        """
        Build the artifact "list" from sources defined in the given configuration.

        :returns: Dictionary descirebed above.
        """
        artifactList = {}
        priority = 0
        for source in self.configuration.artifactSources:
            priority += 1

            if source['type'] == 'mead-tag':
                logging.info("Building artifact list from tag %s", source['tag-name'])
                artifacts = self._listMeadTagArtifacts(source['koji-url'],
                                                       source['download-root-url'],
                                                       source['tag-name'],
                                                       source['included-gav-patterns'])
            elif source['type'] == 'dependency-list':
                logging.info("Building artifact list from top level list of GAVs")
                artifacts = self._listDependencies(source['repo-url'],
                                                   self._parseDepList(source['top-level-gavs']))
            elif source['type'] == 'repository':
                logging.info("Building artifact list from repository %s", source['repo-url'])
                artifacts = self._listRepository(source['repo-url'],
                                                 source['included-gav-patterns'])
            elif source['type'] == 'artifacts':
                logging.info("Building artifact list from list of artifacts")
                artifacts = self._listArtifacts(source['repo-url'],
                                                self._parseDepList(source['included-gavs']))
            else:
                logging.warning("Unsupported source type: %s", source['type'])
                continue

            logging.debug("Placing %d artifacts in the result list", len(artifacts))
            for artifact in artifacts:
                gat = artifact.getGAT()
                artifactList.setdefault(gat, {}).setdefault(priority, {})[artifact.version] = artifacts[artifact]
            logging.debug("The result contains %d GATs so far", len(artifactList))

        return artifactList

    def _listMeadTagArtifacts(self, kojiUrl, downloadRootUrl, tagName, gavPatterns):
        """
        Loads maven artifacts from koji (brew/mead).

        :param kojiUrl: Koji/Brew/Mead URL
        :param downloadRootUrl: Download root URL of the artifacts
        :param tagName: Koji/Brew/Mead tag name
        :returns: Dictionary where index is MavenArtifact object and value is it's repo root URL.
        """

        kojiSession = koji.ClientSession(kojiUrl)
        kojiArtifacts = kojiSession.getLatestMavenArchives(tagName)

        gavsWithExts = {}
        for artifact in kojiArtifacts:
            # FIXME: This isn't reliable as file extension is not equal to
            # maven type, e.g. jar != ejb
            artifactType = re.search('.*\.(.+)$', artifact['filename']).group(1)
            gavUrl = mrbutils.slashAtTheEnd(downloadRootUrl) + artifact['build_name'] + '/'\
                     + artifact['build_version'] + '/' + artifact['build_release'] + '/maven/'
            gavu = (artifact['group_id'], artifact['artifact_id'], artifact['version'], gavUrl)
            gavsWithExts.setdefault(gavu, []).append(artifactType)

        artifacts = {}
        for gavu in gavsWithExts:
            if len(gavsWithExts[gavu]) > 1:
                gavsWithExts[gavu].remove("pom")
            for ext in gavsWithExts[gavu]:
                mavenArtifact = MavenArtifact(gavu[0], gavu[1], ext, gavu[2])
                artifacts[mavenArtifact] = gavu[3]

        return self._filterArtifactsByPatterns(artifacts, gavPatterns)

    def _listDependencies(self, repoUrls, gavs):
        """
        Loads maven artifacts from mvn dependency:list.

        :param repoUrls: URL of the repositories that contains the listed artifacts
        :param gavs: List of top level GAVs
        :returns: Dictionary where index is MavenArtifact object and value is
                  it's repo root URL, or empty dictionary if something goes wrong.
        """
        artifacts = {}

        for gav in gavs:
            artifact = MavenArtifact.createFromGAV(gav)

            pomDir = 'poms'
            fetched = False
            for repoUrl in repoUrls:
                pomUrl = repoUrl + '/' + artifact.getPomFilepath()
                if fetchArtifact(pomUrl, pomDir):
                    fetched = True
                    break

            if not fetched:
                logging.warning("Failed to retrieve pom file for artifact %s",
                                gav)
                continue

            # Build dependency:list
            mvnOutDir = "maven"
            if not os.path.isdir(mvnOutDir):
                os.makedirs(mvnOutDir)
            mvnOutFilename = mvnOutDir + "/" + artifact.getBaseFilename() + "-maven.out"
            with open(mvnOutFilename, "w") as mvnOutputFile:
                retCode = call(['mvn', 'dependency:list', '-N', '-f',
                                pomDir + '/' + artifact.getPomFilename()], stdout=mvnOutputFile)

                if retCode != 0:
                    logging.warning("Maven failed to finish with success. Skipping artifact %s",
                                    gav)
                    continue

            # Parse GAVs from maven output
            gavList = self._parseDepList(mvnOutFilename)

            artifacts.update(self._listArtifacts(repoUrls, gavList))

        return artifacts

    def _listRepository(self, repoUrls, gavPatterns):
        """
        Loads maven artifacts from a repository.

        :param repoUrl: repository URL (local or remote, supported are [file://], http:// and
                        https:// urls)
        :returns: Dictionary where index is MavenArtifact object and value is it's repo root URL.
        """
        artifacts = {}
        for repoUrl in reversed(repoUrls):
            protocol = mrbutils.urlProtocol(repoUrl)
            if protocol == 'file':
                artifacts.update(self._listLocalRepository(repoUrl[7:]))
            elif protocol == '':
                artifacts.update(self._listLocalRepository(repoUrl))
            elif protocol == 'http' or protocol == 'https':
                artifacts.update(self._listRemoteRepository(repoUrl))
            else:
                raise "Invalid protocol!", protocol

        artifacts = self._filterArtifactsByPatterns(artifacts, gavPatterns)
        logging.debug("Found %d artifacts", len(artifacts))

        return artifacts

    def _listRemoteRepository(self, repoUrl, prefix=""):
        artifacts = {}
        (out, _) = Popen(r'lftp -c "set ssl:verify-certificate no ; open ' + repoUrl + prefix
                         + ' ; find  ."', stdout=PIPE, shell=True).communicate()

        # ^./(groupId)/(artifactId)/(version)/(filename)$
        regexGAVF = re.compile(r'\./(.+)/([^/]+)/([^/]+)/([^/]+\.[^/.]+)$')
        gavsWithExts = {}
        for line in out.split('\n'):
            if (line):
                line = "./" + prefix + line[2:]
                gavf = regexGAVF.match(line)
                if gavf is not None:
                    av = re.escape(gavf.group(2) + "-" + gavf.group(3) + ".")
                    regexExt = re.compile(av + self._fileExtRegExp)
                    ext = regexExt.match(gavf.group(4))
                    if ext is not None:
                        gav = (gavf.group(1).replace('/', '.'), gavf.group(2), gavf.group(3))
                        gavsWithExts.setdefault(gav, []).append(ext.group(1))

        for gav in gavsWithExts:
            if len(gavsWithExts[gav]) > 1:
                gavsWithExts[gav].remove("pom")
            for ext in gavsWithExts[gav]:
                mavenArtifact = MavenArtifact(gav[0], gav[1], ext, gav[2])
                artifacts[mavenArtifact] = repoUrl
        return artifacts

    def _listLocalRepository(self, directoryPath, prefix=""):
        """
        Loads maven artifacts from local directory.

        :param directoryPath: Path of the local directory.
        :returns: Dictionary where index is MavenArtifact object and value is it's repo root URL
                  starting with 'file://'.
        """
        artifacts = {}
        # ^(groupId)/(artifactId)/(version)$
        regexGAV = re.compile(r'^(.*)/([^/]*)/([^/]*)$')
        for dirname, dirnames, filenames in os.walk(directoryPath + prefix):
            if not dirnames:
                gavPath = dirname.replace(directoryPath, '')
                gav = regexGAV.search(gavPath)
                av = re.escape(gav.group(2) + "-" + gav.group(3) + ".")
                regexExt = re.compile(av + self._fileExtRegExp)
                exts = []
                for filename in filenames:
                    ext = regexExt.match(filename)
                    if ext is not None:
                        exts.append(ext.group(1))

                if len(exts) > 1:
                    exts.remove("pom")
                for ext in exts:
                    mavenArtifact = MavenArtifact(gav.group(1).replace('/', '.'),
                                                  gav.group(2), ext, gav.group(3))
                    artifacts[mavenArtifact] = "file://" + directoryPath
        return artifacts

    def _listArtifacts(self, urls, gavs):
        """
        Loads maven artifacts from list of GAVs and tries to locate the artifacts in one of the
        specified repositories.

        :param urls: repository URLs where the given GAVs can be located
        :param gavs: List of GAVs
        :returns: Dictionary where index is MavenArtifact object and value is it's repo root URL.
        """
        artifacts = {}
        for gav in gavs:
            artifact = MavenArtifact.createFromGAV(gav)
            for url in urls:
                gavUrl = url + '/' + artifact.getDirPath()
                if mrbutils.urlExists(gavUrl):
                    artifacts[artifact] = url
                    break
            if not artifact in artifacts:
                logging.warning('artifact %s not found in any url!', artifact)

        return artifacts

    def _parseDepList(self, depList):
        """Parse maven dependency:list output and return a list of GAVs"""
        regexComment = re.compile('#.*$')
        # Match pattern groupId:artifactId:[type:][classifier:]version[:scope]
        regexGAV = re.compile('(([\w\-.]+:){2,3}([\w\-.]+:)?([\d][\w\-.]+))(:[\w]*\S)?')
        gavList = []
        for nextLine in depList:
            nextLine = regexComment.sub('', nextLine)
            nextLine = nextLine.strip()
            gav = regexGAV.search(nextLine)
            if gav:
                gavList.append(gav.group(1))

        return gavList

    def _filterArtifactsByPatterns(self, artifacts, gavPatterns):
        if not gavPatterns:
            return artifacts

        includedArtifacts = {}
        for artifact in artifacts:
            matches = map(lambda x: re.match(x, artifact.getGAV()), gavPatterns)
            if any(matches):
                includedArtifacts[artifact] = artifacts[artifact]
        return includedArtifacts
