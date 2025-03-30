"""
Utility Functions Module

This module provides helper functions for file I/O operations and data processing
in the urban drainage model, particularly for SWMM input/output file handling.
"""


def get_swmm_inp_content(filename, flag):
    """
    Extracts content from a specific section of a SWMM input file.
    
    Args:
        filename (str): Path to the SWMM input file
        flag (str): Section identifier (e.g., '[TITLE]', '[JUNCTIONS]')
    
    Returns:
        list: Lines of content from the specified section
    """
    flag += '\n'
    result = []

    with open(filename, 'r', encoding='utf-8') as f:
        # getting to the flag line
        for line in f:
            if line == flag:
                break
        # adding related lines to results
        for line in f:
            # finish when getting to another section
            if line[0] == '[':
                break
            # skip if this line is blank or annotation
            if line == '\n' or line[0] == ';':
                continue
            result.append(line[0:-1])

    return result


def combine_swmm_inp_contents(content1, content2):
    """
    Combines two sections of SWMM input content based on matching identifiers.
    
    Args:
        content1 (list): Primary content lines
        content2 (list): Secondary content lines to merge
    
    Returns:
        list: Combined content with merged information
    """
    # generate a name list of content1
    index_dic = []
    for line in content1:
        pair = line.split()
        index_dic.append(pair[0])
    #
    for line in content2:
        pair = line.split()
        index = index_dic.index(pair[0])
        content1[index] = content1[index] + ' ' + ' '.join(pair[1::])
    #
    return content1


def get_swmm_rpt_content(filename, flag):
    """
    Extracts content from a specific section of a SWMM report file.
    
    Args:
        filename (str): Path to the SWMM report file
        flag (str): Section identifier
    
    Returns:
        list: Lines of content from the specified report section
    """
    # example:
    # res = ut.get_swmm_rpt_content('calculate_temp/test.rpt', 'Node G80F425')
    flag = f'  <<< {flag} >>>\n'
    result = []
    with open(filename, 'r', encoding='utf-8') as f:
        # getting to the flag line
        for line in f:
            if line == flag:
                break
        # adding related lines to results
        i = 0
        for line in f:
            # skip title bar ( four lines )
            if i < 4:
                i += 1
                continue
            # finish when getting to another section
            if line == '  \n':
                break
            result.append(line[0:-1])
    return result
